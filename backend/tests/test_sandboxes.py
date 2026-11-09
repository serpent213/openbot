"""Unprivileged boundary/transaction tests. Same-UID fixtures do NOT certify OS isolation."""
import asyncio
import os
import struct
import sys
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from openbot.config import SandboxResource, Settings
from openbot.db.models import BotScratchpad, Run, Sandbox, SandboxExecution, new_id
from openbot.main import create_app
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.sandboxes import protocol, runtime
from openbot.sandboxes.policy import directory
from openbot.sandboxes.worker import Worker
from tests.conftest import build_test_services
from tests.factories import bot_actor
from tests.fakes import ScriptedChatModel, ai, call

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux peer-credential worker")


@asynccontextmanager
async def worker_at(tmp_path, controller_uid=None):
    home = tmp_path / "home"
    root = home / "workspace"
    root.mkdir(parents=True)
    path = str(tmp_path / "w.sock")
    worker = Worker("slot", root, home, os.getuid() if controller_uid is None else controller_uid)
    server = await asyncio.start_unix_server(worker.handle, path=path)
    async with server:
        yield worker, root, path
    # Let a connection's finally block reap its child before the test loop closes.
    for _ in range(100):
        if not worker.busy:
            break
        await asyncio.sleep(0.01)
    assert not worker.busy


def payload(operation, args=None, **extra):
    return {"id": new_id(), "resource": "slot", "operation": operation, "args": args or {}, **extra}


async def rpc(path, operation, args=None, **extra):
    return await protocol.request(path, os.getuid(), payload(operation, args, **extra))


async def test_real_transport_files_shell_http_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "dummy-server-only")
    monkeypatch.setenv("BASH_ENV", "/this/must/not/be/sourced")
    async with worker_at(tmp_path) as (_, root, path):
        assert "wrote" in await rpc(path, "write_file", {"path": "a.txt", "content": "hello"})
        assert "hello" in await rpc(path, "read_file", {"path": "a.txt"})
        assert "edited" in await rpc(path, "edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "world"})
        assert "a.txt" in await rpc(path, "list_files")
        out = await rpc(path, "run_shell", {"command": 'printf "%s|%s|%s" "$HOME" "${SECRET_KEY-unset}" "${BASH_ENV-unset}"'})
        assert f"{root.parent}|unset|unset" in out
        assert "escapes workspace" in await rpc(path, "read_file", {"path": "../outside"})

        async def respond(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        web = await asyncio.start_server(respond, "127.0.0.1", 0)
        async with web:
            port = web.sockets[0].getsockname()[1]
            assert "hello" in await rpc(path, "http_request", {"method": "GET", "url": f"http://127.0.0.1:{port}"})


async def test_peer_authentication_both_directions(tmp_path):
    async with worker_at(tmp_path, controller_uid=os.getuid() + 1) as (_, root, path):
        with pytest.raises(protocol.SandboxError):
            await rpc(path, "write_file", {"path": "no", "content": "no"})
        assert not (root / "no").exists()
    other = tmp_path / "other"
    other.mkdir()
    async with worker_at(other) as (_, root, path):
        with pytest.raises(protocol.SandboxError, match="identity"):
            await protocol.request(path, os.getuid() + 1, payload("write_file", {"path": "no", "content": "no"}))
        assert not (root / "no").exists()


async def test_protocol_refuses_duplicates_unknown_ops_and_bounds(tmp_path):
    async with worker_at(tmp_path) as (_, _root, path):
        request = payload("write_file", {"path": "once", "content": "once"})
        await protocol.request(path, os.getuid(), request)
        with pytest.raises(protocol.SandboxError):
            await protocol.request(path, os.getuid(), request)
        with pytest.raises(protocol.SandboxError):
            await rpc(path, "patch_file")
        out = await rpc(path, "run_shell", {"command": "head -c 100000 /dev/zero | tr '\\0' x"})
        assert len(out) <= 24000
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(struct.pack("!I", protocol.MAX_FRAME + 1))
        await writer.drain()
        response = await protocol.receive(reader)
        assert response["ok"] is False
        writer.close()
        await writer.wait_closed()


async def test_timeout_does_not_repeat_effect(tmp_path):
    async with worker_at(tmp_path) as (_, root, path):
        request = payload("run_shell", {"command": "echo once >> effect; sleep 20", "timeout": 5})
        with pytest.raises(protocol.SandboxError):
            await protocol.request(path, os.getuid(), request)
        with pytest.raises(protocol.SandboxError):
            await protocol.request(path, os.getuid(), request)
        assert (root / "effect").read_text() == "once\n"


def test_configuration_and_relative_paths(settings):
    with pytest.raises(ValueError, match="OPENBOT_API_KEY"):
        Settings(sandboxes_enabled=True, _env_file=None)
    settings.sandboxes_enabled = True
    settings.openbot_api_key = "test-only"
    settings.sandbox_resources = {"slot": SandboxResource(socket_path="/tmp/test.sock", uid=os.getuid())}
    with pytest.raises(protocol.SandboxError, match="distinct"):
        runtime.resource(settings, "slot")
    for path in ("/etc", "../x", "~/x", "a/../../b", "a\\b", "a\x00b"):
        with pytest.raises(ValueError):
            directory(path)
    assert directory("project/sub") == "project/sub"


async def configure(settings, path, monkeypatch, scripts=None):
    settings.sandboxes_enabled = True
    settings.openbot_api_key = "test-only"
    slot = SandboxResource(socket_path=path, uid=os.getuid())
    settings.sandbox_resources = {"slot": slot}
    # Same-UID unit-test transport ONLY. Production rejects this above; an admin
    # must run the distinct-UID pilot described in docs/sandboxes.md.
    monkeypatch.setattr(runtime, "resource", lambda settings, name: settings.sandbox_resources[name])
    monkeypatch.setattr("openbot.api.sandboxes.resource", runtime.resource)
    services = await build_test_services(settings, scripts)
    async with services.session_factory() as session:
        box = Sandbox(name="pilot", resource="slot", resource_key=f"linux:uid:{os.getuid()}", resource_spec=slot.model_dump())
        session.add(box)
        await session.commit()
        settings.sandbox_default_id = box.id
    return services, box


async def test_internal_loop_crosses_worker_without_host_io_or_global_memory(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path) as (_, root, path):
        scripts = {"eng": [ai(tool_calls=[call("write_file", path="result", content="isolated"),
                                          call("write_file", path="parallel", content="serialized")]),
                            ai(tool_calls=[call("run_shell", command="pwd")]), ai("Done")]}
        services, box = await configure(settings, path, monkeypatch, scripts)
        async with services.session_factory() as session:
            bot = bot_actor("eng", tool_names=["write_file", "run_shell"])
            session.add(bot)
            await session.flush()
            session.add(BotScratchpad(actor_id=bot.id, content="DO-NOT-IMPORT"))
            await session.commit()
            thread = await create_thread(services, session, title="pilot", handles=["eng"], created_by=await human_actor(session))
        def forbidden(*args, **kwargs):
            raise AssertionError("server accessed sandbox filesystem/global memory")
        monkeypatch.setattr("openbot.runtime.runner.thread_workspace_root", forbidden)
        monkeypatch.setattr("openbot.runtime.runner.memory.relevant_memories", forbidden)
        monkeypatch.setattr("openbot.runtime.runner.enabled_skills", forbidden)
        monkeypatch.setattr(services.reflector, "schedule", forbidden)
        ScriptedChatModel.seen.clear()
        await services.actors.start()
        try:
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="go")
            await services.actors.wait_idle(timeout=30)
            async with services.session_factory() as session:
                run = await session.scalar(select(Run).where(Run.thread_id == thread.id))
                assert run.status == "completed", run.error
                assert run.sandbox_id == box.id and run.sandbox_generation == 1
                intents = (await session.scalars(select(SandboxExecution))).all()
                assert {i.operation for i in intents} == {"probe", "write_file", "run_shell"}
                assert all(i.state == "completed" for i in intents)
            assert (root / "result").read_text() == "isolated"
            assert (root / "parallel").read_text() == "serialized"
            assert not (settings.workspace_root / "result").exists()
            assert "DO-NOT-IMPORT" not in str(ScriptedChatModel.seen)
        finally:
            await services.actors.stop()
            await services.http_client.aclose()


@pytest.mark.parametrize("changes", [
    {"tool_names": ["scratchpad"]}, {"tool_names": ["mcp__tool"]},
    {"tool_names": ["sql_query"]}, {"tool_names": ["create_bot"]},
    {"provider": "pi"}, {"provider": "codex"}, {"load_repository_instructions": True},
    {"skill_names": ["private"]}, {"model_settings": {"web_search": True}},
])
def test_unsupported_paths_fail_closed(changes):
    with pytest.raises(protocol.SandboxError):
        runtime.validate_bot(bot_actor("eng", **changes))


async def test_unknown_outcomes_quarantine_and_survive_restart(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "missing.sock"), monkeypatch)
    async with services.session_factory() as session:
        row = await session.get(Sandbox, box.id)
        row.active_run_id = "run"
        await session.commit()
    with pytest.raises(protocol.SandboxError):
        await runtime.execute(services, box.id, "run", "write_file", {"path": "x", "content": "x"}, None)
    async with services.session_factory() as session:
        assert (await session.get(Sandbox, box.id)).state == "quarantined"
        item = await session.scalar(select(SandboxExecution))
        assert item.state == "unknown"
        item.state = "pending"  # crash after intent commit, before recording a result
        await session.commit()
    await runtime.recover(services)
    async with services.session_factory() as session:
        assert (await session.scalar(select(SandboxExecution))).state == "unknown"
        assert (await session.get(Sandbox, box.id)).active_run_id is None
    with pytest.raises(protocol.SandboxError):
        await runtime.execute(services, box.id, "run", "write_file", {}, None)
    await services.http_client.aclose()


async def test_api_binding_allocation_and_quarantine(settings, tmp_path, monkeypatch):
    services, box = await configure(settings, str(tmp_path / "missing.sock"), monkeypatch)
    app = create_app(settings, services=services)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/sandboxes")).status_code == 401
        client.headers["X-API-Key"] = "test-only"
        assert (await client.post("/api/v1/sandboxes", json={"name": "second", "resource": "slot"})).status_code == 409
        created = await client.post("/api/v1/threads", json={"title": "pilot", "sandbox_id": box.id, "working_directory": "project"})
        assert created.status_code == 201, created.text
        assert created.json()["sandbox_id"] == box.id
        thread_id = created.json()["id"]
        assert (await client.patch(f"/api/v1/threads/{thread_id}", json={"sandbox_id": None, "default_bot_handle": "chief_of_staff"})).status_code == 422
        assert (await client.get("/api/v1/workspace/directories", params={"path": "/"})).status_code == 409
        assert (await client.get("/api/v1/providers/cli/claude-code")).status_code == 409
        assert (await client.get("/api/v1/tools/skills")).status_code == 409
        retired = await client.post(f"/api/v1/sandboxes/{box.id}/retire")
        assert retired.json()["state"] == "quarantined"
        assert (await client.post("/api/v1/threads", json={"sandbox_id": box.id})).status_code == 422
        assert (await client.post("/api/v1/sandboxes", json={"name": "recycle", "resource": "slot"})).status_code == 409
    await services.http_client.aclose()
