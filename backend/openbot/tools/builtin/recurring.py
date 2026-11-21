"""Recurring-schedule tools. Unlike schedule_message these are granted per bot in the
bot editor; the interval and count limits are in Settings (recurring_*)."""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from sqlalchemy import select

from openbot.db.models import Actor, RecurringSchedule, Thread
from openbot.runtime import recurring
from openbot.tools.context import RunContext


async def _own(session, ctx: RunContext, schedule_id: str | None = None):
    query = select(RecurringSchedule).where(RecurringSchedule.sender_actor_id == ctx.actor_id)
    if schedule_id is not None:
        query = query.where(RecurringSchedule.id == schedule_id)
    if ctx.sandbox_id:
        query = query.join(Thread, Thread.id == RecurringSchedule.thread_id).where(
            Thread.sandbox_id == ctx.sandbox_id)
    return query


def _line(s: RecurringSchedule) -> str:
    nxt = s.next_due_at.isoformat() if s.next_due_at else "-"
    to = f" to {','.join(s.to_handles)}" if s.to_handles else ""
    return f"{s.id} {s.status} '{s.cron}' {s.timezone} next {nxt}{to} thread {s.thread_id}: {s.content[:80]}"


async def _publish(ctx: RunContext, schedule_id: str, thread_id: str, status: str) -> None:
    await ctx.services.bus.publish("scheduled.updated", thread_id, {"recurring_id": schedule_id, "status": status})


@tool
async def schedule_recurring(cron: str, content: str, runtime: ToolRuntime[RunContext], timezone: str = "",
                             to: list[str] | None = None) -> str:
    """Post a message in this thread repeatedly, on a five-field cron rule
    (minute hour day-of-month month day-of-week), e.g. "0 9 * * 1-5" for weekdays at 09:00.
    timezone is an IANA name such as "Europe/Berlin" (server default if empty); `to` lists
    bot handles to wake. Firings are posted by @cron. Check list_recurring before adding
    a duplicate; there is a minimum interval and a per-bot limit.
    """
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        actor = await session.get(Actor, ctx.actor_id)
        try:
            schedule = await recurring.create(session, ctx.services.settings, actor=actor,
                                              thread_id=ctx.thread_id, content=content, expression=cron,
                                              timezone=timezone or None, to=to)
        except recurring.RecurringError as exc:
            return f"Error: {exc}"
        line = _line(schedule)
        schedule_id, thread_id = schedule.id, schedule.thread_id
    await _publish(ctx, schedule_id, thread_id, "active")
    return f"Created recurring schedule {line}"


@tool
async def list_recurring(runtime: ToolRuntime[RunContext]) -> str:
    """List your recurring schedules with their rule, status and next firing."""
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        rows = (await session.execute((await _own(session, ctx)).order_by(RecurringSchedule.created_at))).scalars()
        return "\n".join(_line(s) for s in rows) or "(none)"


@tool
async def pause_recurring(schedule_id: str, runtime: ToolRuntime[RunContext], resume: bool = False) -> str:
    """Pause one of your recurring schedules, or resume it with resume=true.
    Resuming continues from now; firings missed while paused are not sent.
    """
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        schedule = (await session.execute(await _own(session, ctx, schedule_id))).scalar_one_or_none()
        if schedule is None:
            return "Error: recurring schedule not found"
        actor = await session.get(Actor, ctx.actor_id)
        try:
            schedule = await recurring.edit(session, ctx.services.settings, schedule, actor, paused=not resume)
        except recurring.RecurringError as exc:
            return f"Error: {exc}"
        line, thread_id = _line(schedule), schedule.thread_id
    await _publish(ctx, schedule_id, thread_id, "active" if resume else "paused")
    return line


@tool
async def cancel_recurring(schedule_id: str, runtime: ToolRuntime[RunContext]) -> str:
    """Delete one of your recurring schedules for good. Messages it already posted stay."""
    ctx = runtime.context
    async with ctx.services.session_factory() as session:
        schedule = (await session.execute(await _own(session, ctx, schedule_id))).scalar_one_or_none()
        if schedule is None:
            return "Error: recurring schedule not found"
        thread_id = schedule.thread_id
        await recurring.remove(session, schedule)
    await _publish(ctx, schedule_id, thread_id, "deleted")
    return f"Deleted recurring schedule {schedule_id}"


RECURRING_TOOLS = [schedule_recurring, list_recurring, pause_recurring, cancel_recurring]
