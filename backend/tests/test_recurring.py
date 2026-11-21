from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from openbot.db.models import Actor, Message, RecurringSchedule, ScheduledMessage, Thread
from openbot.runtime import recurring
from openbot.runtime.scheduler import Scheduler
from openbot.tools.builtin.recurring import (
    cancel_recurring,
    list_recurring,
    pause_recurring,
    schedule_recurring,
)
from openbot.tools.builtin.scheduling import cancel_scheduled_message

BASE = "/api/v1/scheduled/recurring"


def _as(actor="you"):
    return {"X-OpenBot-Actor": actor}


async def _thread(client, *handles):
    for handle in handles or ("eng",):
        await client.post("/api/v1/bots", json={"handle": handle, "name": handle.title(), "provider": "openai", "model": "x"})
    return (await client.post("/api/v1/threads", json={"handles": list(handles or ("eng",))})).json()


def _body(thread_id, **extra):
    return {"thread_id": thread_id, "content": "standup?", "cron": "0 9 * * 1-5", "timezone": "Europe/Berlin",
            "to": ["eng"], **extra}


async def test_create_list_preview_pause_resume_delete(client):
    thread = await _thread(client)
    assert (await client.get(BASE)).status_code == 401
    created = await client.post(BASE, headers=_as(), json=_body(thread["id"]))
    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["status"] == "active" and schedule["next_due_at"]
    due = datetime.fromisoformat(schedule["next_due_at"])
    assert due.astimezone(recurring.cron.zone("Europe/Berlin")).hour == 9
    assert [s["id"] for s in (await client.get(BASE, headers=_as())).json()] == [schedule["id"]]

    preview = (await client.get(f"{BASE}/preview", params={"cron": "0 9 * * 1-5", "timezone": "Europe/Berlin"})).json()
    assert len(preview["next"]) == 5 and preview["next"][0] == schedule["next_due_at"]
    assert (await client.get(f"{BASE}/preview", params={"cron": "61 * * * *"})).status_code == 422

    path = f"{BASE}/{schedule['id']}"
    paused = (await client.patch(path, headers=_as(), json={"paused": True})).json()
    assert paused["status"] == "paused" and paused["next_due_at"] is None
    resumed = (await client.patch(path, headers=_as(), json={"paused": False, "cron": "30 8 * * *"})).json()
    assert resumed["status"] == "active" and resumed["cron"] == "30 8 * * *" and resumed["next_due_at"]
    assert (await client.patch(path, headers=_as(), json={"cron": "bad"})).status_code == 422
    assert (await client.get(path, headers=_as())).json()["cron"] == "30 8 * * *"

    assert (await client.delete(path, headers=_as())).status_code == 204
    assert (await client.get(path, headers=_as())).status_code == 404


async def test_validation(client):
    thread = await _thread(client)
    for bad in ({"cron": "* * *"}, {"timezone": "Mars/Base"}, {"cron": "0 0 31 2 *"}, {"to": ["ghost"]},
                {"thread_id": "missing"}):
        response = await client.post(BASE, headers=_as(), json={**_body(thread["id"]), **bad})
        assert response.status_code in (403, 404, 422), (bad, response.text)
    # The server default timezone fills in a missing one.
    body = _body(thread["id"])
    del body["timezone"]
    assert (await client.post(BASE, headers=_as(), json=body)).json()["timezone"] == "UTC"


async def test_bot_limits_and_visibility(client, settings):
    thread = await _thread(client, "eng", "qa")
    # People may schedule often; bots only every recurring_min_interval_seconds.
    assert (await client.post(BASE, headers=_as(), json=_body(thread["id"], cron="*/5 * * * *"))).status_code == 201
    too_often = await client.post(BASE, headers=_as("eng"), json=_body(thread["id"], cron="0,10 * * * *"))
    assert too_often.status_code == 422 and "15 min" in too_often.text
    settings.recurring_max_per_bot = 2
    for _ in range(2):
        assert (await client.post(BASE, headers=_as("eng"), json=_body(thread["id"], cron="*/15 * * * *"))).status_code == 201
    assert (await client.post(BASE, headers=_as("eng"), json=_body(thread["id"]))).status_code == 409
    # A bot sees and manages only its own; a person sees all of them.
    eng = (await client.get(BASE, headers=_as("eng"))).json()
    assert len(eng) == 2
    assert len((await client.get(BASE, headers=_as())).json()) == 3
    assert (await client.get(BASE, headers=_as("qa"))).json() == []
    assert (await client.delete(f"{BASE}/{eng[0]['id']}", headers=_as("qa"))).status_code == 404
    assert (await client.delete(f"{BASE}/{eng[0]['id']}", headers=_as())).status_code == 204


async def _schedule(services, *, next_due_at, cron="0 9 * * *", status="active"):
    async with services.session_factory() as session:
        you = (await session.execute(select(Actor).where(Actor.handle == "you"))).scalar_one()
        thread = Thread(title="recurring")
        session.add(thread)
        await session.flush()
        schedule = RecurringSchedule(thread_id=thread.id, sender_actor_id=you.id, to_handles=[], content="tick",
                                     cron=cron, timezone="UTC", status=status, next_due_at=next_due_at)
        session.add(schedule)
        await session.commit()
        return schedule.id, thread.id


async def _get(services, schedule_id):
    async with services.session_factory() as session:
        schedule = await session.get(RecurringSchedule, schedule_id)
        jobs = (await session.execute(select(ScheduledMessage).where(
            ScheduledMessage.recurring_id == schedule_id))).scalars().all()
        return schedule, jobs


NOW = datetime(2026, 11, 21, 9, 5, tzinfo=UTC)


async def test_fires_once_and_advances(services):
    schedule_id, _ = await _schedule(services, next_due_at=NOW - timedelta(minutes=5))
    assert len(await recurring.fire_due(services, NOW)) == 1
    assert await recurring.fire_due(services, NOW) == []  # not again
    schedule, jobs = await _get(services, schedule_id)
    assert schedule.next_due_at == datetime(2026, 11, 22, 9, tzinfo=UTC)
    assert schedule.last_fired_at == datetime(2026, 11, 21, 9, tzinfo=UTC) and schedule.skipped == 0
    assert [(j.content, j.status, j.due_at) for j in jobs] == [("tick", "pending", schedule.last_fired_at)]
    assert schedule.last_message_id == jobs[0].id


async def test_downtime_catches_up_the_latest_firing_only(services):
    # Down for three days, back 5 minutes after today's firing: one message, two skipped.
    schedule_id, _ = await _schedule(services, next_due_at=NOW - timedelta(days=3, minutes=5))
    await recurring.fire_due(services, NOW)
    schedule, jobs = await _get(services, schedule_id)
    assert len(jobs) == 1 and jobs[0].due_at == datetime(2026, 11, 21, 9, tzinfo=UTC)
    assert schedule.skipped == 3


async def test_firing_older_than_grace_is_skipped(services):
    schedule_id, _ = await _schedule(services, next_due_at=datetime(2026, 11, 21, 7, tzinfo=UTC), cron="0 7 * * *")
    await recurring.fire_due(services, NOW)  # 2 h 5 min late, grace is 1 h
    schedule, jobs = await _get(services, schedule_id)
    assert jobs == [] and schedule.skipped == 1 and schedule.next_due_at == datetime(2026, 11, 22, 7, tzinfo=UTC)


async def test_paused_and_deleted_thread(services):
    paused_id, _ = await _schedule(services, next_due_at=NOW - timedelta(minutes=1), status="paused")
    gone_id, thread_id = await _schedule(services, next_due_at=NOW - timedelta(minutes=1))
    async with services.session_factory() as session:
        (await session.get(Thread, thread_id)).deleted_at = NOW
        await session.commit()
    assert await recurring.fire_due(services, NOW) == []
    assert (await _get(services, paused_id))[1] == []
    assert (await _get(services, gone_id))[0] is None


async def test_scheduler_delivers_a_firing_in_the_same_sweep(services, monkeypatch):
    schedule_id, _ = await _schedule(services, next_due_at=datetime.now(UTC) - timedelta(seconds=1),
                                     cron="* * * * *")
    posted = []

    async def deliver(*args, **kwargs):
        posted.append(kwargs["meta"])
        return SimpleNamespace(message=Message(id="m1", thread_id=kwargs["thread_id"], sender_kind="bot",
                                               sender_name="cron", content=kwargs["content"]))

    monkeypatch.setattr("openbot.runtime.scheduler.post_message", deliver)
    await Scheduler(services).run_due()
    _, jobs = await _get(services, schedule_id)
    assert [j.status for j in jobs] == ["delivered"] and posted == [{"scheduled_message_id": jobs[0].id}]


async def test_delete_cancels_an_undelivered_firing(services):
    schedule_id, _ = await _schedule(services, next_due_at=NOW - timedelta(minutes=1))
    [job_id] = await recurring.fire_due(services, NOW)
    async with services.session_factory() as session:
        await recurring.remove(session, await session.get(RecurringSchedule, schedule_id))
        job = await session.get(ScheduledMessage, job_id)
        assert job.status == "cancelled" and job.recurring_id == schedule_id


async def test_bot_tools(client, services):
    thread = await _thread(client)
    async with services.session_factory() as session:
        eng = (await session.execute(select(Actor).where(Actor.handle == "eng"))).scalar_one()
    ctx = SimpleNamespace(services=services, actor_id=eng.id, actor_handle="eng", thread_id=thread["id"],
                          sandbox_id=None)
    runtime = SimpleNamespace(context=ctx)
    assert (await schedule_recurring.coroutine("*/5 * * * *", "poll", runtime)).startswith("Error: fires every 5 min")
    created = await schedule_recurring.coroutine("0 9 * * 1-5", "standup?", runtime, timezone="Europe/Berlin")
    assert created.startswith("Created recurring schedule ")
    schedule_id = created.split()[3]
    assert schedule_id in await list_recurring.coroutine(runtime)
    assert " paused " in await pause_recurring.coroutine(schedule_id, runtime)
    assert " active " in await pause_recurring.coroutine(schedule_id, runtime, resume=True)
    other = SimpleNamespace(context=SimpleNamespace(**{**vars(ctx), "actor_id": "someone-else"}))
    assert await cancel_recurring.coroutine(schedule_id, other) == "Error: recurring schedule not found"
    assert (await cancel_recurring.coroutine(schedule_id, runtime)).startswith("Deleted")
    assert await list_recurring.coroutine(runtime) == "(none)"


async def test_cancel_scheduled_message_tool(client, services):
    thread = await _thread(client)
    async with services.session_factory() as session:
        eng = (await session.execute(select(Actor).where(Actor.handle == "eng"))).scalar_one()
        job = ScheduledMessage(thread_id=thread["id"], sender_actor_id=eng.id, to_handles=[], content="later",
                               due_at=datetime.now(UTC) + timedelta(hours=1))
        session.add(job)
        await session.commit()
        job_id = job.id
    runtime = SimpleNamespace(context=SimpleNamespace(services=services, actor_id=eng.id, sandbox_id=None))
    assert (await cancel_scheduled_message.coroutine(job_id, runtime)).startswith("Cancelled")
    assert await cancel_scheduled_message.coroutine(job_id, runtime) == "Error: it is already cancelled"
