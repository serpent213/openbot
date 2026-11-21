from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from openbot.api.deps import get_services, get_session
from openbot.db.models import (
    Actor,
    RecurringSchedule,
    ScheduledMessage,
    Thread,
    ThreadParticipant,
    utcnow,
)
from openbot.runtime import recurring
from openbot.services import Services

router = APIRouter(prefix="/scheduled", tags=["scheduled"])


async def scheduled_actor(actor_handle: str | None = Header(default=None, alias="X-OpenBot-Actor"),
                          session=Depends(get_session)) -> Actor:
    """Resolve the explicit operator identity; never infer an actor."""
    if not actor_handle:
        raise HTTPException(401, "X-OpenBot-Actor is required for scheduled messages")
    actor = (await session.execute(select(Actor).where(Actor.handle == actor_handle))).scalar_one_or_none()
    if actor is None or not actor.enabled:
        raise HTTPException(403, "unknown or disabled actor")
    return actor


class Create(BaseModel):
    thread_id: str
    content: str = Field(min_length=1, max_length=20_000)
    due_at: datetime
    to: list[str] = Field(default_factory=list, max_length=20)


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    thread_id: str
    content: str
    due_at: datetime
    status: str
    attempts: int
    last_error: str | None
    result_message_id: str | None
    to_handles: list[str]


async def _validate_target(session, thread_id: str, handles: list[str]) -> None:
    thread = await session.get(Thread, thread_id)
    if thread is None or thread.deleted_at is not None:
        raise HTTPException(404, "thread not found")
    actors = {a.handle: a for a in (await session.execute(select(Actor))).scalars()}
    unknown = [h for h in handles if h not in actors or actors[h].kind != "bot" or not actors[h].enabled]
    if unknown:
        raise HTTPException(422, f"unknown or unavailable bot handles: {unknown}")
    participant_ids = set((await session.execute(
        select(ThreadParticipant.actor_id).where(ThreadParticipant.thread_id == thread_id)
    )).scalars())
    not_in_thread = [h for h in handles if actors[h].id not in participant_ids]
    if not_in_thread:
        raise HTTPException(422, f"recipients are not participants in the target thread: {not_in_thread}")


async def _owned(session, job_id: str, actor: Actor) -> ScheduledMessage:
    job = (await session.execute(select(ScheduledMessage).where(
        ScheduledMessage.id == job_id, ScheduledMessage.sender_actor_id == actor.id
    ))).scalar_one_or_none()
    if job is None:
        raise HTTPException(404, "scheduled message not found")
    return job


@router.get("", response_model=list[Out])
async def list_scheduled(actor: Actor = Depends(scheduled_actor), session=Depends(get_session)):
    return (await session.execute(select(ScheduledMessage).where(
        ScheduledMessage.sender_actor_id == actor.id
    ).order_by(ScheduledMessage.due_at.desc()).limit(200))).scalars().all()


@router.get("/{job_id}", response_model=Out)
async def inspect(job_id: str, actor: Actor = Depends(scheduled_actor), session=Depends(get_session)):
    return await _owned(session, job_id, actor)


@router.post("", response_model=Out, status_code=201)
async def create(body: Create, actor: Actor = Depends(scheduled_actor), session=Depends(get_session),
                 services: Services = Depends(get_services)):
    due = body.due_at.astimezone(UTC) if body.due_at.tzinfo else body.due_at.replace(tzinfo=UTC)
    if due <= utcnow():
        raise HTTPException(422, "due_at must be in the future")
    await _validate_target(session, body.thread_id, body.to)
    participant = await session.scalar(select(ThreadParticipant.actor_id).where(
        ThreadParticipant.thread_id == body.thread_id, ThreadParticipant.actor_id == actor.id
    ))
    if participant is None:
        raise HTTPException(403, "actor is not a participant in the target thread")
    job = ScheduledMessage(thread_id=body.thread_id, sender_actor_id=actor.id, to_handles=body.to,
                           content=body.content, due_at=due)
    session.add(job)
    await session.commit()
    await session.refresh(job)
    await services.bus.publish("scheduled.updated", body.thread_id, {"id": job.id, "status": job.status})
    return job


@router.post("/{job_id}/cancel", response_model=Out)
async def cancel(job_id: str, actor: Actor = Depends(scheduled_actor), session=Depends(get_session),
                 services: Services = Depends(get_services)):
    job = await _owned(session, job_id, actor)
    if job.status == "pending":
        job.status = "cancelled"
        await session.commit()
        await services.bus.publish("scheduled.updated", job.thread_id, {"id": job.id, "status": job.status})
        await session.refresh(job)
    return job


# ── Recurring schedules ──────────────────────────────────────────────────────
# A separate router, included before the one above so /scheduled/recurring is not
# taken for a one-shot job ID. People see and manage every schedule (to stop a bot's
# runaway rule, say); a bot sees only its own.

recurring_router = APIRouter(prefix="/scheduled/recurring", tags=["scheduled"])


class RecurringCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str
    content: str = Field(min_length=1, max_length=recurring.MAX_CONTENT)
    cron: str = Field(min_length=1, max_length=120, description="Five fields: minute hour day month weekday")
    timezone: str | None = Field(default=None, max_length=64, description="IANA zone; the server default if omitted")
    to: list[str] = Field(default_factory=list, max_length=recurring.MAX_RECIPIENTS)


class RecurringUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None = Field(default=None, min_length=1, max_length=recurring.MAX_CONTENT)
    cron: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    to: list[str] | None = Field(default=None, max_length=recurring.MAX_RECIPIENTS)
    paused: bool | None = None


class RecurringOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    thread_id: str
    sender_actor_id: str
    to_handles: list[str]
    content: str
    cron: str
    timezone: str
    status: str
    next_due_at: datetime | None
    last_fired_at: datetime | None
    last_message_id: str | None
    skipped: int
    created_at: datetime
    updated_at: datetime


class Preview(BaseModel):
    cron: str
    timezone: str
    next: list[datetime]


def _http(exc: recurring.RecurringError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


async def _schedule(session, schedule_id: str, actor: Actor) -> RecurringSchedule:
    schedule = await session.get(RecurringSchedule, schedule_id)
    if schedule is None or (actor.kind != "human" and schedule.sender_actor_id != actor.id):
        raise HTTPException(404, "recurring schedule not found")
    return schedule


async def _publish_recurring(services: Services, schedule_id: str, thread_id: str, status: str) -> None:
    await services.bus.publish("scheduled.updated", thread_id, {"recurring_id": schedule_id, "status": status})


@recurring_router.get("", response_model=list[RecurringOut])
async def list_recurring(actor: Actor = Depends(scheduled_actor), session=Depends(get_session)):
    """List recurring schedules: all of them for a person, its own for a bot."""
    query = select(RecurringSchedule).order_by(RecurringSchedule.created_at)
    if actor.kind != "human":
        query = query.where(RecurringSchedule.sender_actor_id == actor.id)
    return (await session.execute(query)).scalars().all()


@recurring_router.get("/preview", response_model=Preview)
async def preview_recurring(cron: str, timezone: str | None = None, count: int = 5,
                            services: Services = Depends(get_services)):
    """Show the next firings of a rule without saving it."""
    timezone = timezone or services.settings.default_timezone
    try:
        times = recurring.preview(cron, timezone, max(1, min(count, 20)))
    except recurring.RecurringError as exc:
        raise _http(exc) from exc
    return Preview(cron=cron, timezone=timezone, next=times)


@recurring_router.post("", response_model=RecurringOut, status_code=201)
async def create_recurring(body: RecurringCreate, actor: Actor = Depends(scheduled_actor),
                           session=Depends(get_session), services: Services = Depends(get_services)):
    """Post a message on a cron rule, e.g. `0 9 * * 1-5` for weekdays at 09:00."""
    participant = await session.scalar(select(ThreadParticipant.actor_id).where(
        ThreadParticipant.thread_id == body.thread_id, ThreadParticipant.actor_id == actor.id))
    if participant is None:
        raise HTTPException(403, "actor is not a participant in the target thread")
    try:
        schedule = await recurring.create(session, services.settings, actor=actor, thread_id=body.thread_id,
                                          content=body.content, expression=body.cron,
                                          timezone=body.timezone, to=body.to)
    except recurring.RecurringError as exc:
        raise _http(exc) from exc
    await _publish_recurring(services, schedule.id, schedule.thread_id, schedule.status)
    return schedule


@recurring_router.get("/{schedule_id}", response_model=RecurringOut)
async def get_recurring(schedule_id: str, actor: Actor = Depends(scheduled_actor), session=Depends(get_session)):
    """Show one recurring schedule."""
    return await _schedule(session, schedule_id, actor)


@recurring_router.patch("/{schedule_id}", response_model=RecurringOut)
async def update_recurring(schedule_id: str, body: RecurringUpdate, actor: Actor = Depends(scheduled_actor),
                           session=Depends(get_session), services: Services = Depends(get_services)):
    """Edit, pause (`paused: true`) or resume a schedule. Resuming does not catch up missed firings."""
    schedule = await _schedule(session, schedule_id, actor)
    try:
        schedule = await recurring.edit(session, services.settings, schedule, actor, content=body.content,
                                        expression=body.cron, timezone=body.timezone, to=body.to,
                                        paused=body.paused)
    except recurring.RecurringError as exc:
        await session.rollback()
        raise _http(exc) from exc
    await _publish_recurring(services, schedule.id, schedule.thread_id, schedule.status)
    return schedule


@recurring_router.delete("/{schedule_id}", status_code=204, response_class=Response)
async def delete_recurring(schedule_id: str, actor: Actor = Depends(scheduled_actor),
                           session=Depends(get_session), services: Services = Depends(get_services)):
    """Delete a schedule. Messages it already posted stay; an undelivered firing is cancelled."""
    schedule = await _schedule(session, schedule_id, actor)
    thread_id = schedule.thread_id
    await recurring.remove(session, schedule)
    await _publish_recurring(services, schedule_id, thread_id, "deleted")
    return Response(status_code=204)
