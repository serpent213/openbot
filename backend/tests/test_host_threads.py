"""Explicit operator host execution, without worker/UID or live-service requirements."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from openbot.config import SandboxResource
from openbot.db.models import Actor, Run, Sandbox, Thread
from openbot.runtime.commands import CommandContext, NoArguments, new_command
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.sandboxes import runtime
from tests.factories import bot_actor
from tests.fakes import ai, call


async def enable(services, client, monkeypatch):
    settings = services.settings
    settings.sandboxes_enabled = True
    settings.openbot_api_key = "test-only"
    client.headers["X-API-Key"] = "test-only"
    settings.workspace_root.mkdir(exist_ok=True)
    slot = SandboxResource(socket_path="/not/a/live/worker.sock", uid=62000)
    settings.sandbox_resources = {"slot": slot}
    monkeypatch.setattr(runtime, "resource", lambda settings, name: settings.sandbox_resources[name])
    async with services.session_factory() as session:
        box = Sandbox(name="Development", resource="slot", resource_key="test:slot", resource_spec=slot.model_dump())
        session.add(box)
        await session.commit()
        settings.sandbox_default_id = box.id
    return box


async def test_explicit_selection_default_and_immutable_binding(client, services, monkeypatch):
    box = await enable(services, client, monkeypatch)
    inventory = (await client.get("/api/v1/sandboxes")).json()
    assert inventory["default_sandbox_id"] == box.id
    assert inventory["host_execution_allowed"] is False
    denied = await client.post("/api/v1/threads", json={"host_execution": True})
    assert denied.status_code == 422 and "disabled" in denied.text
    services.settings.sandbox_allow_host_execution = True
    for body in ({}, {"sandbox_id": None}, {"host_execution": False}):
        result = await client.post("/api/v1/threads", json=body)
        assert result.status_code == 201, result.text
        assert result.json()["sandbox_id"] == box.id
        assert result.json()["host_execution"] is False
    host = await client.post("/api/v1/threads", json={"host_execution": True})
    assert host.status_code == 201, host.text
    thread = host.json()
    assert thread["host_execution"] is True and thread["sandbox_id"] is None
    assert (await client.get(f"/api/v1/threads/{thread['id']}")).json()["host_execution"] is True
    assert (await client.post("/api/v1/threads", json={"host_execution": True, "sandbox_id": box.id})).status_code == 422
    assert (await client.post("/api/v1/threads", json={"sandbox_id": "missing"})).status_code == 422
    assert (await client.patch(f"/api/v1/threads/{thread['id']}", json={"host_execution": False})).status_code == 422
    services.settings.sandbox_default_id = None
    assert (await client.post("/api/v1/threads", json={})).status_code == 422
    assert (await client.post("/api/v1/threads", json={"host_execution": True})).status_code == 201


async def test_host_never_implied_by_old_null_binding_and_run_snapshot_must_match(settings):
    services = SimpleNamespace(settings=settings)
    settings.sandboxes_enabled = True
    settings.sandbox_allow_host_execution = True
    thread = SimpleNamespace(sandbox_id=None, host_execution=False)
    run = SimpleNamespace(sandbox_id=None, host_execution=False)
    with pytest.raises(runtime.SandboxRejected, match="local fallback refused"):
        async with runtime.binding(services, run, thread):
            pytest.fail("old unbound thread must remain blocked")
    thread.host_execution = True
    with pytest.raises(runtime.SandboxRejected, match="snapshot|selection"):
        async with runtime.binding(services, run, thread):
            pytest.fail("run snapshot must match")
    run.host_execution = True
    async with runtime.binding(services, run, thread):
        pass
    settings.sandbox_allow_host_execution = False
    with pytest.raises(runtime.SandboxRejected, match="disabled"):
        async with runtime.binding(services, run, thread):
            pytest.fail("operator can disable future execution, including resumes")
    settings.sandbox_allow_host_execution = True
    thread.sandbox_id = "sandbox"
    with pytest.raises(runtime.SandboxRejected, match="cannot have a sandbox"):
        async with runtime.binding(services, run, thread):
            pytest.fail("ambiguous bindings must fail")


async def test_host_run_uses_existing_tools_and_snapshots_intent(client, services, scripts, monkeypatch):
    await enable(services, client, monkeypatch)
    services.settings.sandbox_allow_host_execution = True
    scripts["eng"] = [ai(tool_calls=[call("write_file", path="host-result", content="host")]), ai("Done")]
    async with services.session_factory() as session:
        session.add(bot_actor("eng", tool_names=["write_file"]))
        await session.commit()
    def no_worker(*a, **kw):
        pytest.fail("explicit host turn must not use a worker")
    monkeypatch.setattr(runtime, "execute", no_worker)
    result = await client.post("/api/v1/threads", json={"host_execution": True, "handles": ["eng"], "default_bot_handle": "eng"})
    assert result.status_code == 201, result.text
    thread = result.json()
    await client.post(f"/api/v1/threads/{thread['id']}/messages", json={"content": "go"})
    await services.actors.wait_idle()
    async with services.session_factory() as session:
        run = await session.scalar(select(Run).where(Run.thread_id == thread["id"]))
        assert run.status == "completed", run.error
        assert run.host_execution and run.sandbox_id is None
    assert (services.settings.workspace_root / "host-result").read_text() == "host"


async def test_agent_cannot_create_host_thread_and_new_preserves_operator_choice(client, services, monkeypatch):
    await enable(services, client, monkeypatch)
    services.settings.sandbox_allow_host_execution = True
    async with services.session_factory() as session:
        bot = bot_actor("eng")
        session.add(bot)
        await session.commit()
        with pytest.raises(ValueError, match="only the operator"):
            await create_thread(services, session, title="escape", handles=[], created_by=bot, host_execution=True)
        human = await human_actor(session)
        thread = await create_thread(services, session, title="host", handles=[], created_by=human, host_execution=True)
        ctx = CommandContext(services, session, human, thread.id, operator=True)
        result = await new_command(ctx, NoArguments())
        new = await session.get(Thread, result.thread_id)
        assert new.host_execution and new.sandbox_id is None
        denied = await new_command(CommandContext(services, session, bot, thread.id, operator=False), NoArguments())
        # An unprivileged actor cannot inherit host access; no additional host thread.
        assert denied.thread_id is None or not (await session.get(Thread, denied.thread_id)).host_execution


async def test_host_browsing_and_probes_need_explicit_operator_opt_in(client, services, monkeypatch):
    await enable(services, client, monkeypatch)
    for endpoint in ("workspace/directories", "tools/skills", "providers/cli/pi"):
        assert (await client.get(f"/api/v1/{endpoint}")).status_code == 409
        assert (await client.get(f"/api/v1/{endpoint}?host_execution=true")).status_code == 403
        assert (await client.get(f"/api/v1/{endpoint}?host_execution=true&sandbox_id=any")).status_code == 422
    services.settings.sandbox_allow_host_execution = True
    (services.settings.workspace_root / "project").mkdir()
    result = await client.get("/api/v1/workspace/directories?host_execution=true")
    assert result.status_code == 200, result.text
    assert result.json()["entries"] == [{"name": "project", "path": "project"}]
    assert (await client.get("/api/v1/workspace/directories")).status_code == 409


async def test_host_runs_respect_global_and_per_bot_admission(client, services, monkeypatch):
    await enable(services, client, monkeypatch)
    services.settings.sandbox_allow_host_execution = True
    services.actors.sem = asyncio.Semaphore(1)
    threads = {}
    async with services.session_factory() as session:
        session.add_all([bot_actor("one"), bot_actor("two")])
        await session.commit()
        for name in ("one", "two"):
            threads[name] = await create_thread(services, session, title=name, handles=[name],
                default_bot_handle=name, created_by=await human_actor(session), host_execution=True)
    started = {name: asyncio.Event() for name in threads}
    release = asyncio.Event()
    active = set()

    async def execute(run_id, resume):
        async with services.session_factory() as session:
            run = await session.get(Run, run_id)
            actor = await session.get(Actor, run.actor_id)
        assert not active
        active.add(actor.handle)
        started[actor.handle].set()
        await release.wait()
        active.remove(actor.handle)
        await services.runner._set_status(run.id, "completed")

    monkeypatch.setattr(services.runner, "_execute", execute)
    async def post(name):
        async with services.session_factory() as session:
            await post_message(services, session, thread_id=threads[name].id,
                               sender=await human_actor(session), content="go")
    try:
        await post("one")
        await asyncio.wait_for(started["one"].wait(), 5)
        await post("two")
        # Wait for the second run to be picked; it must wait outside _execute.
        async with asyncio.timeout(5):
            while True:
                async with services.session_factory() as session:
                    if await session.scalar(select(Run.id).where(Run.thread_id == threads["two"].id)):
                        break
                await asyncio.sleep(.01)
        assert not started["two"].is_set()
        release.set()
        await services.actors.wait_idle()
        assert started["two"].is_set()
    finally:
        release.set()
