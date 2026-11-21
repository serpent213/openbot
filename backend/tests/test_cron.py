from datetime import UTC, datetime, timedelta

import pytest

from openbot.runtime.cron import CronError, min_gap, next_after, parse, upcoming, zone

BERLIN = zone("Europe/Berlin")


def local(*args, tz=BERLIN, fold=0):
    return datetime(*args, tzinfo=tz, fold=fold)


def nxt(expr, after, tz=BERLIN):
    return next_after(parse(expr), after, tz)


def test_parse_fields_names_steps_and_aliases():
    spec = parse("*/15 9-17 * jan-mar,dec MON-fri")
    assert spec.minutes == (0, 15, 30, 45)
    assert spec.hours == tuple(range(9, 18))
    assert spec.months == {1, 2, 3, 12}
    assert spec.weekdays == {1, 2, 3, 4, 5}
    assert parse("0 0 * * 7").weekdays == {0}
    assert parse("5/20 * * * *").minutes == (5, 25, 45)
    assert parse("@daily").minutes == (0,) and parse("@daily").hours == (0,)


@pytest.mark.parametrize("expr", [
    "", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
    "* * * * 8", "5-1 * * * *", "*/0 * * * *", "1,,2 * * * *", "x * * * *", "0 0 31 2 *",
    "0 0 30 feb *",
])
def test_parse_rejects(expr):
    with pytest.raises(CronError):
        parse(expr)


def test_unknown_timezone():
    with pytest.raises(CronError):
        zone("Mars/Olympus")


def test_weekdays_at_nine_across_a_weekend():
    friday = local(2026, 11, 20, 9, 0)
    assert nxt("0 9 * * 1-5", friday) == local(2026, 11, 23, 9, 0)
    assert nxt("0 9 * * 1-5", friday - timedelta(minutes=1)) == friday


def test_result_is_strictly_after_and_in_utc():
    result = nxt("30 8 * * *", local(2026, 11, 21, 8, 30))
    assert result == local(2026, 11, 22, 8, 30) and result.tzinfo == UTC


def test_day_of_month_or_day_of_week():
    # 13th of the month OR any Friday, as in classic cron.
    spec = parse("0 12 13 * fri")
    times = upcoming(spec, local(2026, 11, 1), BERLIN, 4)
    assert [t.astimezone(BERLIN).day for t in times] == [6, 13, 20, 27]
    # Day of month alone; February 29 only in leap years.
    assert nxt("0 0 29 2 *", local(2026, 3, 1)) == local(2028, 2, 29)


def test_spring_forward_gap_fires_at_first_valid_minute():
    # 2026-03-29: Berlin jumps from 02:00 to 03:00.
    assert nxt("30 2 * * *", local(2026, 3, 28, 3)) == local(2026, 3, 29, 3, 0)
    assert nxt("30 2 * * *", local(2026, 3, 29, 3, 0)) == local(2026, 3, 30, 2, 30)
    # With a wildcard hour the missing minutes are skipped, not bunched up.
    times = upcoming(parse("*/30 * * * *"), local(2026, 3, 29, 1, 0), BERLIN, 3)
    assert [t.astimezone(BERLIN).strftime("%H:%M") for t in times] == ["01:30", "03:00", "03:30"]


def test_fall_back_repeated_hour_fires_once_unless_hour_is_wildcard():
    # 2026-10-25: Berlin repeats 02:00-03:00.
    # Compare in UTC: Python never equates a repeated-hour time across zones (PEP 495).
    first = local(2026, 10, 25, 2, 30, fold=0).astimezone(UTC)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert nxt("30 2 * * *", local(2026, 10, 25, 1)) == first
    assert nxt("30 2 * * *", first) == local(2026, 10, 26, 2, 30)
    hourly = upcoming(parse("30 * * * *"), local(2026, 10, 25, 1, 0), BERLIN, 4)
    assert [t - hourly[0] for t in hourly] == [timedelta(hours=h) for h in range(4)]


def test_other_timezones_and_utc():
    assert nxt("0 9 * * *", datetime(2026, 11, 21, 0, tzinfo=UTC), UTC) == datetime(2026, 11, 21, 9, tzinfo=UTC)
    kolkata = zone("Asia/Kolkata")
    assert nxt("0 9 * * *", datetime(2026, 11, 21, 0, tzinfo=UTC), kolkata) == datetime(2026, 11, 21, 3, 30, tzinfo=UTC)


def test_min_gap():
    after = local(2026, 11, 21)
    assert min_gap(parse("0 9 * * 1-5"), after, BERLIN, below=timedelta(minutes=15)) == timedelta(days=1)
    assert min_gap(parse("0,5 * * * *"), after, BERLIN, below=timedelta(minutes=15)) == timedelta(minutes=5)
    assert min_gap(parse("* * * * *"), after, BERLIN, below=timedelta(minutes=15)) == timedelta(minutes=1)
    assert min_gap(parse("0 0 1 1 *"), after, BERLIN, below=timedelta(minutes=15)) is None


def test_frequent_rules_are_quick():
    import time
    start = time.monotonic()
    upcoming(parse("* * * * *"), local(2026, 11, 21), BERLIN, 500)
    min_gap(parse("*/15 * * * *"), local(2026, 11, 21), BERLIN, below=timedelta(minutes=15))
    assert time.monotonic() - start < 2
