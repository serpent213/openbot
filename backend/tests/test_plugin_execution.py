"""Trusted plugins stay on the controller; only helper calls cross the worker boundary.

Worker integration below uses disposable same-UID sockets/fake cgroups, not a UID-isolation proof.
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from openbot.db.models import Run, SandboxExecution
from openbot.plugin_api import ProcessExecutionError, execute_process
from openbot.runtime.processes import ProcessRequest, run_process
from openbot.sandboxes import protocol, runtime
from openbot.tools.context import RunContext
from openbot.tools.registry import build_registry
from tests.factories import bot_actor
from tests.test_sandbox_lifecycle import seed
from tests.test_sandboxes import configure, worker_at


async def test_process_limits_stdin_exit_status_and_environment(tmp_path):
    result = await run_process(ProcessRequest(argv=[sys.executable, "-c",
        ("import sys,os; print(sys.stdin.read()); print(os.environ['EXPLICIT']); "
        "print('x'*200); print('y'*200,file=sys.stderr); sys.exit(7)")],
        stdin="input", env={"EXPLICIT": "supplied"}, output_limit=40), cwd=str(tmp_path), env={})
    assert result.returncode == 7 and not result.timed_out
    assert result.stdout.startswith("input\nsupplied")
    assert len(result.stdout) == len(result.stderr) == 40
    assert result.stdout_truncated and result.stderr_truncated


async def test_process_timeout_and_cancellation_reap_child(tmp_path):
    from tests.test_builtin_tools import alive
    pidfile = tmp_path / "pid"
    request = ProcessRequest(argv=[sys.executable, "-c",
        "import os,time,pathlib; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(30)"], timeout=.5)
    result = await run_process(request, cwd=str(tmp_path), env={})
    assert result.timed_out and not alive(int(pidfile.read_text()))
    pidfile.unlink()
    task = asyncio.create_task(run_process(request.model_copy(update={"timeout": 30}), cwd=str(tmp_path), env={}))
    async with asyncio.timeout(10):
        while not pidfile.exists():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(int(pidfile.read_text()))


@tool
async def trusted_example() -> str:
    """Trusted server plugin example."""
    return "controller operation"


def test_trusted_tools_and_chief_work_without_weakening_worker_routes(settings):
    registry = build_registry(settings, plugins=[])
    registry.register(trusted_example, source="plugin:example")
    names = ["create_bot", "read_bot_description", "read_bot_instructions", "update_bot_description",
             "update_bot_instructions", "scratchpad", "trusted_example", "run_shell"]
    runtime.validate_bot(bot_actor("chief", tool_names=names), settings, registry)
    tools = {t.name: t for t in runtime.tools(names, registry)}
    assert tools["trusted_example"] is trusted_example
    assert tools["create_bot"] is registry.get("create_bot")
    assert tools["run_shell"].coroutine is not registry.get("run_shell").coroutine
    registry.register(trusted_example, source="mcp:host-server")
    with pytest.raises(protocol.SandboxRejected, match="trusted_example"):
        runtime.validate_bot(bot_actor("chief", tool_names=names), settings, registry)
    with pytest.raises(protocol.SandboxRejected):
        runtime.tools(["trusted_example"], registry)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker transport")
async def test_helper_uses_active_worker_and_never_inherits_controller_secret(settings, tmp_path, monkeypatch):
    from sqlalchemy import select
    monkeypatch.setenv("PLUGIN_PRIVATE_KEY", "controller-only-fake")
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        bot, thread, run = await seed(services, box)
        async with services.session_factory() as session:
            (await session.get(Run, run.id)).status = "running"
            await session.commit()
        ctx = RunContext(bot.id, bot.handle, bot.name, thread.id, run.id, tmp_path / "wrong-host-root", services, sandbox_id=box.id)
        rt = SimpleNamespace(context=ctx)
        try:
            async with runtime.binding(services, run, thread, bot):
                result = await execute_process(rt, [sys.executable, "-c",
                    ("import os,json,pathlib; pathlib.Path('worker-proof').write_text('yes'); "
                    "print(json.dumps([os.getcwd(),os.getenv('PLUGIN_PRIVATE_KEY'),os.getenv('EXPLICIT')]))")],
                    env={"EXPLICIT": "fake-grant"})
                assert json.loads(result.stdout) == [str(root), None, "fake-grant"]
                assert (root / "worker-proof").read_text() == "yes"
                assert not ctx.workspace_root.exists()
                with pytest.raises(ProcessExecutionError):
                    await execute_process(rt, ["openbot-missing-executable-test"])
                assert (await execute_process(rt, [sys.executable, "-c", "print('still usable')"])).stdout.strip() == "still usable"
                with pytest.raises(protocol.SandboxRejected):
                    await execute_process(rt, [sys.executable, "-c", "raise AssertionError"], cwd="../escape")
                ctx.sandbox_id = None
                with pytest.raises(protocol.SandboxRejected, match="matching active"):
                    await execute_process(rt, [sys.executable, "-c", "raise AssertionError"])
                ctx.sandbox_id = box.id
            # Stale calls must not reacquire a run lease or fall back to Host.
            with pytest.raises(protocol.SandboxRejected, match="not active"):
                await execute_process(rt, [sys.executable, "-c", "raise AssertionError"])
            async with services.session_factory() as session:
                assert {e.operation for e in (await session.scalars(select(SandboxExecution))).all()} >= {"execute_process"}
        finally:
            await services.http_client.aclose()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worker transport")
@pytest.mark.parametrize("approval", [False, True])
async def test_chief_and_plugin_tools_run_with_existing_approval_policy(settings, tmp_path, monkeypatch, approval):
    from sqlalchemy import select

    from openbot.db.models import RunEvent
    from openbot.runtime.delivery import create_thread, human_actor, post_message
    from tests.fakes import ai, call

    async with worker_at(tmp_path, leased=False) as (_, _root, path):
        services, _box = await configure(settings, path, monkeypatch, {"chief": [
            ai(tool_calls=[call("trusted_example")]),
            ai(tool_calls=[call("read_bot_description", bot="chief")]), ai("Ready"),
        ]})
        services.registry.register(trusted_example, source="plugin:example")
        async with services.session_factory() as session:
            session.add(bot_actor("chief", tool_names=["create_bot", "read_bot_description", "read_bot_instructions",
                "update_bot_description", "update_bot_instructions", "scratchpad", "trusted_example"],
                approval_tools=["trusted_example"] if approval else []))
            await session.commit()
            thread = await create_thread(services, session, title="chief", handles=["chief"],
                default_bot_handle="chief", created_by=await human_actor(session))
        await services.actors.start()
        try:
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="go")
            await services.actors.wait_idle(timeout=30)
            async with services.session_factory() as session:
                run = await session.scalar(select(Run).where(Run.thread_id == thread.id))
                assert run.status == ("waiting_human" if approval else "completed"), run.error
                events = (await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all()
                if not approval:
                    assert {e.payload.get("name") for e in events if e.type == "tool_call"} >= {"read_bot_description", "trusted_example"}
        finally:
            await services.actors.stop()
            await services.http_client.aclose()


async def test_helper_preserves_non_sandbox_host_execution(services, tmp_path, monkeypatch):
    from openbot.runtime.delivery import create_thread, human_actor
    monkeypatch.setenv("PLUGIN_PRIVATE_KEY", "not-inherited")
    async with services.session_factory() as session:
        bot = bot_actor("host-plugin")
        session.add(bot)
        await session.commit()
        thread = await create_thread(services, session, title="host", handles=[bot.handle], created_by=await human_actor(session))
        run = Run(actor_id=bot.id, thread_id=thread.id, status="running")
        session.add(run)
        await session.commit()
    ctx = RunContext(bot.id, bot.handle, bot.name, thread.id, run.id, tmp_path, services)
    result = await execute_process(SimpleNamespace(context=ctx), [sys.executable, "-c",
        "import os,json; print(json.dumps([os.getcwd(),os.getenv('PLUGIN_PRIVATE_KEY')]))"])
    assert json.loads(result.stdout) == [str(tmp_path), None]
    services.settings.sandboxes_enabled = True
    with pytest.raises(protocol.SandboxRejected, match="unbound"):
        await execute_process(SimpleNamespace(context=ctx), [sys.executable, "-c", "raise AssertionError"])
