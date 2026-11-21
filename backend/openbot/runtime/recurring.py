"""Recurring schedules: a cron rule that posts the same message again and again.

Each firing is written as an ordinary ScheduledMessage (with ``recurring_id`` set),
so the existing scheduler delivers it as @cron with its retries and duplicate
protection, and every outcome stays visible. The HTTP API and the bot tools share
the operations here; limits on interval and count apply to bots only.

Missed firings (the server was down): only the latest one is caught up, and only
if it is less than ``recurring_grace_seconds`` late. Older ones are counted in
``skipped``; a schedule never fires in a burst.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from openbot.db.models import (
    Actor,
    RecurringSchedule,
    ScheduledMessage,
    Thread,
    ThreadParticipant,
    utcnow,
)
from openbot.runtime import cron

log = logging.getLogger(__name__)

MAX_CONTENT = 20_000
MAX_RECIPIENTS = 20
# Bound the catch-up scan after a long outage (a 15-minute rule over a month is ~2900).
_CATCH_UP_LIMIT = 10_000


class RecurringError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _is_bot(actor: Actor) -> bool:
    return actor.kind == "bot"


def check_rule(expression: str, timezone: str, actor: Actor, settings, now: datetime | None = None):
    """Parse and vet a rule; returns (spec, zone, first firing)."""
    try:
        spec = cron.parse(expression)
        tz = cron.zone(timezone)
    except cron.CronError as exc:
        raise RecurringError(str(exc)) from exc
    now = now or utcnow()
    first = cron.next_after(spec, now, tz)
    if first is None:
        raise RecurringError("the rule never fires")
    if _is_bot(actor):
        minimum = timedelta(seconds=settings.recurring_min_interval_seconds)
        gap = cron.min_gap(spec, now, tz, below=minimum)
        if gap is not None and gap < minimum:
            raise RecurringError(
                f"fires every {_human(gap)}; bots may schedule at most every {_human(minimum)}")
    return spec, tz, first


def _human(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" + (f" {rest} min" if rest else "")


def preview(expression: str, timezone: str, count: int = 5, now: datetime | None = None) -> list[datetime]:
    try:
        spec, tz = cron.parse(expression), cron.zone(timezone)
    except cron.CronError as exc:
        raise RecurringError(str(exc)) from exc
    return cron.upcoming(spec, now or utcnow(), tz, count)


async def check_recipients(session, thread_id: str, handles: list[str]) -> None:
    thread = await session.get(Thread, thread_id)
    if thread is None or thread.deleted_at is not None:
        raise RecurringError("thread not found", 404)
    if len(handles) > MAX_RECIPIENTS:
        raise RecurringError(f"at most {MAX_RECIPIENTS} recipients")
    actors = {a.handle: a for a in (await session.execute(select(Actor))).scalars()}
    unknown = [h for h in handles if h not in actors or actors[h].kind != "bot" or not actors[h].enabled]
    if unknown:
        raise RecurringError(f"unknown or unavailable bot handles: {unknown}")
    participants = set((await session.execute(
        select(ThreadParticipant.actor_id).where(ThreadParticipant.thread_id == thread_id))).scalars())
    outside = [h for h in handles if actors[h].id not in participants]
    if outside:
        raise RecurringError(f"recipients are not participants in the target thread: {outside}")


def _check_content(content: str) -> str:
    if not content.strip() or len(content) > MAX_CONTENT:
        raise RecurringError(f"content must be 1..{MAX_CONTENT} characters")
    return content


async def create(session, settings, *, actor: Actor, thread_id: str, content: str, expression: str,
                 timezone: str | None = None, to: list[str] | None = None) -> RecurringSchedule:
    timezone = timezone or settings.default_timezone
    recipients = list(dict.fromkeys(to or []))
    _check_content(content)
    _, _, first = check_rule(expression, timezone, actor, settings)
    await check_recipients(session, thread_id, recipients)
    if _is_bot(actor):
        owned = await session.scalar(select(func.count()).select_from(RecurringSchedule).where(
            RecurringSchedule.sender_actor_id == actor.id))
        if owned >= settings.recurring_max_per_bot:
            raise RecurringError(
                f"this bot already has {owned} recurring schedules (limit {settings.recurring_max_per_bot}); "
                "cancel one first", 409)
    schedule = RecurringSchedule(thread_id=thread_id, sender_actor_id=actor.id, to_handles=recipients,
                                 content=content, cron=" ".join(expression.split()), timezone=timezone,
                                 status="active", next_due_at=first)
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def edit(session, settings, schedule: RecurringSchedule, actor: Actor, *, content: str | None = None,
               expression: str | None = None, timezone: str | None = None, to: list[str] | None = None,
               paused: bool | None = None) -> RecurringSchedule:
    """Change a schedule. The rules are checked against ``actor``, the one making the change."""
    if content is not None:
        schedule.content = _check_content(content)
    if to is not None:
        recipients = list(dict.fromkeys(to))
        await check_recipients(session, schedule.thread_id, recipients)
        schedule.to_handles = recipients
    rule_changed = expression is not None or timezone is not None
    if expression is not None:
        schedule.cron = " ".join(expression.split())
    if timezone is not None:
        schedule.timezone = timezone
    if paused is not None:
        schedule.status = "paused" if paused else "active"
    if schedule.status == "paused":
        if rule_changed:
            check_rule(schedule.cron, schedule.timezone, actor, settings)
        schedule.next_due_at = None
    elif rule_changed or paused is False or schedule.next_due_at is None:
        # Resuming starts from now: nothing missed while paused is caught up.
        _, _, schedule.next_due_at = check_rule(schedule.cron, schedule.timezone, actor, settings)
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def remove(session, schedule: RecurringSchedule) -> None:
    """Delete a schedule and cancel a firing that has not been delivered yet."""
    await session.execute(update(ScheduledMessage).where(
        ScheduledMessage.recurring_id == schedule.id, ScheduledMessage.status == "pending",
    ).values(status="cancelled", last_error="recurring schedule deleted"))
    await session.delete(schedule)
    await session.commit()


def _latest_due(spec, tz, due: datetime, now: datetime) -> tuple[datetime, int]:
    """The last firing at or before ``now`` from ``due`` on, and how many came before it."""
    latest, passed = due, 0
    for _ in range(_CATCH_UP_LIMIT):
        nxt = cron.next_after(spec, latest, tz)
        if nxt is None or nxt > now:
            break
        latest, passed = nxt, passed + 1
    return latest, passed


async def fire_due(services, now: datetime | None = None) -> list[str]:
    """Turn every due schedule into a ScheduledMessage; returns their IDs."""
    now = now or utcnow()
    grace = timedelta(seconds=services.settings.recurring_grace_seconds)
    async with services.session_factory() as session:
        ids = (await session.execute(select(RecurringSchedule.id).where(
            RecurringSchedule.status == "active", RecurringSchedule.next_due_at <= now,
        ).order_by(RecurringSchedule.next_due_at).limit(50))).scalars().all()
    fired: list[str] = []
    for schedule_id in ids:
        async with services.session_factory() as session:
            schedule = await session.get(RecurringSchedule, schedule_id)
            if schedule is None or schedule.status != "active" or schedule.next_due_at is None \
                    or schedule.next_due_at > now:
                continue
            due = schedule.next_due_at
            thread = await session.get(Thread, schedule.thread_id)
            if thread is None or thread.deleted_at is not None:
                log.info("recurring schedule %s removed: its thread is gone", schedule.id)
                await remove(session, schedule)
                continue
            try:
                spec, tz = cron.parse(schedule.cron), cron.zone(schedule.timezone)
            except cron.CronError as exc:  # e.g. a timezone that left the tz database
                log.warning("recurring schedule %s paused: %s", schedule.id, exc)
                schedule.status, schedule.next_due_at = "paused", None
                await session.commit()
                continue
            latest, passed = _latest_due(spec, tz, due, now)
            fire = now - latest <= grace
            values = {"next_due_at": cron.next_after(spec, now, tz),
                      "skipped": schedule.skipped + passed + (0 if fire else 1)}
            job = None
            if fire:
                job = ScheduledMessage(thread_id=schedule.thread_id, sender_actor_id=schedule.sender_actor_id,
                                       to_handles=list(schedule.to_handles), content=schedule.content,
                                       due_at=latest, recurring_id=schedule.id)
                session.add(job)
                await session.flush()
                values |= {"last_fired_at": latest, "last_message_id": job.id}
            if values["next_due_at"] is None:
                values["status"] = "paused"
            # Conditional on the due time we read, so a concurrent sweep or edit wins cleanly.
            result = await session.execute(update(RecurringSchedule).where(
                RecurringSchedule.id == schedule.id, RecurringSchedule.status == "active",
                RecurringSchedule.next_due_at == due,
            ).values(**values, updated_at=utcnow()).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                await session.rollback()
                continue
            await session.commit()
            if job is not None:
                fired.append(job.id)
            elif passed or not fire:
                log.info("recurring schedule %s skipped a firing missed by %s", schedule.id, now - latest)
    return fired
