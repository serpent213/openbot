"""Five-field cron expressions and their next occurrence in a timezone.

Deliberately small instead of a dependency: minute, hour, day of month, month and
day of week, each a ``*``, a number, a range ``a-b``, a list ``a,b`` or a step
``*/n`` / ``a-b/n``; month and weekday names (``jan``, ``mon``); weekday 0 or 7 for
Sunday; and the ``@hourly`` ... ``@yearly`` shorthands. As in classic cron, when both
day of month and day of week are restricted, a day matching either one fires.

Daylight saving time, evaluated on the schedule's wall clock:

* A time that does not exist (02:30 when clocks jump forward) fires at the first
  minute after the gap. If the hour field is ``*``, the missing minutes are skipped.
* A time that happens twice (the repeated hour in autumn) fires once, on its first
  pass. If the hour field is ``*``, it fires on both passes, like real time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["CronError", "CronSpec", "min_gap", "next_after", "parse", "upcoming", "zone"]

_MONTHS = {n: i for i, n in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAYS = {n: i for i, n in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
            "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
            "@annually": "0 0 1 1 *"}
# (name, low, high, names) per field, in expression order.
_FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS))
# A rule that never matches (31 February) would scan forever; give up after this long.
_HORIZON_DAYS = 5 * 366 + 1
_SLACK = timedelta(hours=3)


class CronError(ValueError):
    """An expression or timezone that cannot be used."""


@dataclass(frozen=True)
class CronSpec:
    expression: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool  # day of month is *
    any_weekday: bool  # day of week is *

    @property
    def any_hour(self) -> bool:
        return len(self.hours) == 24

    def matches_date(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom, dow = d.day in self.days, d.isoweekday() % 7 in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow


def _value(token: str, field: str, low: int, high: int, names: dict[str, int]) -> int:
    token = token.lower()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise CronError(f"{field}: {token!r} is not a number" + (" or name" if names else ""))
    number = int(token)
    if not low <= number <= high:
        raise CronError(f"{field}: {number} is outside {low}-{high}")
    return number


def _field(text: str, field: str, low: int, high: int, names: dict[str, int]) -> tuple[set[int], bool]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{field}: empty list item")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{field}: step {step_text!r} must be a positive number")
            step = int(step_text)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, field, low, high, names), _value(b, field, low, high, names)
            if start > end:
                raise CronError(f"{field}: range {base!r} runs backwards")
        else:
            start = _value(base, field, low, high, names)
            end = high if step_text else start  # "5/15" means 5-max/15, as in Vixie cron
        values.update(range(start, end + 1, step))
    return values, text == "*"


def parse(expression: str) -> CronSpec:
    text = " ".join(expression.split())
    text = _ALIASES.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise CronError("expected five fields: minute hour day-of-month month day-of-week")
    parsed = [_field(p, *spec) for p, spec in zip(parts, _FIELDS, strict=True)]
    (minutes, _), (hours, _), (days, any_day), (months, _), (weekdays, any_weekday) = parsed
    weekdays = {0 if d == 7 else d for d in weekdays}
    any_weekday = any_weekday or weekdays == set(range(7))
    any_day = any_day or days == set(range(1, 32))
    if not any_day and any_weekday and not any(
        d <= _MONTH_DAYS[m] for d in days for m in months
    ):
        raise CronError("the day of month never occurs in the chosen months")
    return CronSpec(expression=" ".join(expression.split()), minutes=tuple(sorted(minutes)),
                    hours=tuple(sorted(hours)), days=frozenset(days), months=frozenset(months),
                    weekdays=frozenset(weekdays), any_day=any_day, any_weekday=any_weekday)


_MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone {name!r}") from exc


def _wall(day: date, hour: int = 0, minute: int = 0) -> datetime:
    """A naive wall-clock time; the zone is applied per candidate (fold matters)."""
    return datetime(day.year, day.month, day.day, hour, minute)  # noqa: DTZ001


def _exists(wall: datetime, tz: ZoneInfo) -> bool:
    return wall.replace(tzinfo=tz).astimezone(UTC).astimezone(tz).replace(tzinfo=None) == wall


def _instants(spec: CronSpec, wall: datetime, tz: ZoneInfo) -> list[datetime]:
    """The real instants (UTC) at which one wall-clock match fires."""
    if not _exists(wall, tz):
        if spec.any_hour:
            return []
        after = wall
        for _ in range(24 * 60):  # first minute after the gap; gaps are at most a few hours
            after += timedelta(minutes=1)
            if _exists(after, tz):
                return [after.replace(tzinfo=tz).astimezone(UTC)]
        return []
    first = wall.replace(tzinfo=tz, fold=0)
    second = wall.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset() and spec.any_hour:
        return [first.astimezone(UTC), second.astimezone(UTC)]
    return [first.astimezone(UTC)]


def next_after(spec: CronSpec, after: datetime, tz: ZoneInfo) -> datetime | None:
    """The first firing strictly after ``after`` (aware), as an aware UTC datetime."""
    after = after.astimezone(UTC)
    after_wall = after.astimezone(tz).replace(tzinfo=None)
    # Wall clock and real time differ by at most a DST shift (and a gap shifts a time
    # forward), so a match more than _SLACK before ``after`` cannot fire after it, and
    # once one is found, a match more than _SLACK later cannot fire earlier.
    floor = after_wall - _SLACK
    best: datetime | None = None
    best_wall = after_wall
    day = floor.date()
    for _ in range(_HORIZON_DAYS):
        if best is not None and _wall(day) > best_wall + _SLACK:
            break
        if spec.matches_date(day):
            for h in spec.hours:
                for m in spec.minutes:
                    wall = _wall(day, h, m)
                    if wall <= floor:
                        continue
                    if best is not None and wall > best_wall + _SLACK:
                        return best
                    for t in _instants(spec, wall, tz):
                        if t > after and (best is None or t < best):
                            best, best_wall = t, wall
        day += timedelta(days=1)
    return best


def upcoming(spec: CronSpec, after: datetime, tz: ZoneInfo, count: int) -> list[datetime]:
    result: list[datetime] = []
    current = after
    while len(result) < count:
        nxt = next_after(spec, current, tz)
        if nxt is None:
            break
        result.append(nxt)
        current = nxt
    return result


def min_gap(spec: CronSpec, after: datetime, tz: ZoneInfo, *, below: timedelta,
            span: timedelta = timedelta(days=8)) -> timedelta | None:
    """Shortest interval between firings over the next ``span``; None if it fires at most once.

    Stops at the first interval shorter than ``below``. Eight days cover every weekly
    pattern and a DST change near ``after``.
    """
    shortest: timedelta | None = None
    previous = next_after(spec, after, tz)
    end = after + span
    while previous is not None and previous <= end:
        nxt = next_after(spec, previous, tz)
        if nxt is None or nxt > end:
            break
        gap = nxt - previous
        shortest = gap if shortest is None else min(shortest, gap)
        if shortest < below:
            break
        previous = nxt
    return shortest
