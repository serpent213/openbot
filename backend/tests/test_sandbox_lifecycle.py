"""Durable binding lifecycle; all databases/resources here are disposable fixtures."""
import asyncio
import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from openbot.config import SandboxResource
from openbot.db.models import InboxItem, Run, Sandbox, SandboxExecution, Thread
from openbot.main import create_app
from openbot.runtime.commands import CommandContext, NoArguments, new_command
from openbot.runtime.delivery import create_thread, human_actor
from openbot.sandboxes import protocol, runtime
from tests.factories import bot_actor
from tests.test_sandboxes import configure, payload, worker_at

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox prototype")


async def fake_response(*args, **kwargs):
    return "ready"


async def seed(services, box):
    async with services.session_factory() as session:
        bot = bot_actor("eng", tool_names=[])
        session.add(bot)
        await session.commit()
        thread = await create_thread(services, session, title="bound", handles=["eng"],
                                     created_by=await human_actor(session), sandbox_id=box.id)
        run = Run(actor_id=bot.id, thread_id=thread.id, status="queued",
                  sandbox_id=box.id, sandbox_generation=box.generation)
        session.add(run)
        await session.commit()
    return bot, thread, run


async def test_run_serialization_and_snapshot_fencing(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "absent"), monkeypatch)
    monkeypatch.setattr(protocol, "request", fake_response)
    from tests.sandbox_fakes import fake_lease
    monkeypatch.setattr(protocol, "run_lease", fake_lease)
    bot, thread, first = await seed(services, box)
    async with services.session_factory() as session:
        second = Run(actor_id=bot.id, thread_id=thread.id, status="queued",
                     sandbox_id=box.id, sandbox_generation=box.generation)
        session.add(second)
        await session.commit()
    entered = asyncio.Event()

    async def other():
        async with runtime.binding(services, second, thread, bot):
            entered.set()

    async with runtime.binding(services, first, thread, bot):
        task = asyncio.create_task(other())
        await asyncio.sleep(0.2)
        assert not entered.is_set()
    await asyncio.wait_for(task, 3)
    assert entered.is_set()
    second.sandbox_generation = 999
    with pytest.raises(protocol.SandboxError, match="stale"):
        async with runtime.binding(services, second, thread, bot):
            pytest.fail("stale binding ran")
    second.sandbox_generation = box.generation
    settings.sandbox_resources["slot"] = SandboxResource(socket_path="/changed", uid=12345)
    with pytest.raises(protocol.SandboxError, match="inventory changed"):
        async with runtime.binding(services, second, thread, bot):
            pytest.fail("resource was silently rebound")
    await services.http_client.aclose()


async def test_no_unbound_or_disabled_fallback(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "absent"), monkeypatch)
    bot, _thread, run = await seed(services, box)
    # Use real policy (configure's test-only same-UID shim must not hide disabled mode).
    monkeypatch.undo()
    settings.sandboxes_enabled = False
    await services.runner.execute(run.id)
    async with services.session_factory() as session:
        failed = await session.get(Run, run.id)
        assert failed.status == "failed" and "disabled" in failed.error
        session.add(Thread(id="unbound", title="legacy"))
        await session.flush()
        local = Run(actor_id=bot.id, thread_id="unbound", status="queued")
        session.add(local)
        await session.commit()
    settings.sandboxes_enabled = True
    await services.runner.execute(local.id)
    async with services.session_factory() as session:
        failed = await session.get(Run, local.id)
        assert failed.status == "failed" and "fallback refused" in failed.error
        assert not (await session.scalars(select(SandboxExecution))).all()
    await services.http_client.aclose()


async def test_completed_inbox_is_not_reexecuted_after_restart(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "absent"), monkeypatch)
    bot, thread, run = await seed(services, box)
    async with services.session_factory() as session:
        row = await session.get(Run, run.id)
        row.status = "completed"
        session.add(InboxItem(actor_id=bot.id, thread_id=thread.id, run_id=run.id,
                              kind="message", status="processing"))
        await session.commit()
    await services.actors.start()
    await services.actors.wait_idle()
    try:
        async with services.session_factory() as session:
            assert len((await session.scalars(select(Run))).all()) == 1
            assert (await session.scalar(select(InboxItem).where(InboxItem.run_id == run.id))).status == "done"
            assert (await session.get(Sandbox, box.id)).resource_spec == box.resource_spec
    finally:
        await services.actors.stop()
        await services.http_client.aclose()


async def test_new_command_inherits_binding_not_new_default(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "absent"), monkeypatch)
    _bot, thread, _run = await seed(services, box)
    settings.sandbox_default_id = "not-the-current-binding"
    async with services.session_factory() as session:
        ctx = CommandContext(services, session, await human_actor(session), thread.id, operator=True)
        result = await new_command(ctx, NoArguments())
        created = await session.get(Thread, result.thread_id)
        assert created.sandbox_id == box.id
        await session.rollback()
    await services.http_client.aclose()


async def test_concurrent_allocation_and_uid_alias_are_exclusive(settings, tmp_path, monkeypatch):
    services, _box = await configure(settings, str(tmp_path / "absent"), monkeypatch)
    settings.sandbox_resources["second"] = SandboxResource(socket_path="/second", uid=23456)
    settings.sandbox_resources["alias"] = SandboxResource(socket_path="/second", uid=23456)
    app = create_app(settings, services=services)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-API-Key": "test-only"}) as client:
        replies = await asyncio.gather(*[
            client.post("/api/v1/sandboxes", json={"name": name, "resource": "second"})
            for name in ("a", "b")])
        assert sorted(r.status_code for r in replies) == [201, 409]
        alias = await client.post("/api/v1/sandboxes", json={"name": "alias", "resource": "alias"})
        assert alias.status_code == 409
    await services.http_client.aclose()


async def test_disconnect_cancels_worker_child(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path) as (worker, root, path):
        request = payload("run_shell", {"command": "echo started > started; sleep 30; echo bad > after"})
        task = asyncio.create_task(protocol.request(path, os.getuid(), request))
        for _ in range(200):
            if (root / "started").exists():
                break
            await asyncio.sleep(0.05)
        assert (root / "started").exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not worker.operations:
                break
            await asyncio.sleep(0.02)
        assert not worker.operations
        assert not (root / "after").exists()
