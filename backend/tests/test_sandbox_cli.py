"""Real adapter/protocol/subprocess tests with fake CLIs; no native login/model calls."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from openbot.db.models import BotScratchpad, Run, RunEvent, Sandbox, SandboxExecution
from openbot.runtime import memory
from openbot.runtime.delivery import create_thread, human_actor, post_message
from tests.factories import bot_actor
from tests.test_sandboxes import configure, worker_at

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux worker transport")
SESSION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(params=["claude-code", "pi"])
def backend(request):
    if request.param == "pi":
        pytest.importorskip("openbot_pi", reason="install sibling openbot-pi in the test venv for adapter integration")
    return request.param


def fake_cli(path, root, provider, expected_env=None):
    path.write_text(f"#!{sys.executable}\nROOT={str(root)!r}\nPROVIDER={provider!r}\nSESSION={SESSION!r}\nEXPECTED_ENV={expected_env or {}!r}\n" + r'''
import json, os, stat, sys, time
from pathlib import Path
assert all(os.environ.get(key) == value for key, value in EXPECTED_ENV.items()), "missing explicit app credential"
args = sys.argv[1:]
if "--version" in args:
    print("9.99.99")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    sys.exit(0)
flag = "--append-system-prompt" if PROVIDER == "pi" else "--append-system-prompt-file"
prompt_file = args[args.index(flag) + 1]
resume_flag = "--session" if PROVIDER == "pi" else "--resume"
prompt = sys.stdin.read()
record = {"prompt": prompt, "system": Path(prompt_file).read_text(), "temporary": prompt_file,
          "mode": stat.S_IMODE(os.stat(prompt_file).st_mode), "cwd": os.getcwd(), "home": os.environ["HOME"],
          "env_keys": sorted(os.environ), "resumed": args[args.index(resume_flag) + 1] if resume_flag in args else None}
with (Path(ROOT) / "calls.jsonl").open("a") as f:
    f.write(json.dumps(record) + "\n")
with (Path(ROOT) / "effects").open("a") as f:
    f.write("once\n")
def emit(value):
    print(json.dumps(value), flush=True)
if PROVIDER == "pi":
    emit({"type": "session", "id": SESSION})
    emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "worker streaming"}})
else:
    emit({"type": "system", "subtype": "init", "session_id": SESSION, "cwd": os.getcwd(), "tools": []})
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
          "delta": {"type": "text_delta", "text": "worker streaming"}}})
if "WAIT-FOREVER" in prompt:
    (Path(ROOT) / "started").touch()
    time.sleep(60)
if PROVIDER == "pi":
    emit({"type": "tool_execution_start", "toolCallId": "worker-tool", "toolName": "bash", "args": {"command": "echo ok"}})
    emit({"type": "tool_execution_end", "toolCallId": "worker-tool", "toolName": "bash",
          "result": {"content": [{"type": "text", "text": "ok"}]}})
    emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "worker reply"}],
          "usage": {"input": 5, "output": 2, "totalTokens": 7}}})
    emit({"type": "agent_settled"})
else:
    emit({"type": "assistant", "message": {"role": "assistant", "content": [
          {"type": "text", "text": "worker reply"},
          {"type": "tool_use", "id": "worker-tool", "name": "Bash", "input": {"command": "echo ok"}}]}})
    emit({"type": "user", "message": {"role": "user", "content": [
          {"type": "tool_result", "tool_use_id": "worker-tool", "content": "ok"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "worker reply",
          "session_id": SESSION, "usage": {"input_tokens": 5, "output_tokens": 2}})
''')
    path.chmod(0o755)


async def setup(settings, path, monkeypatch, provider):
    settings.plugin_modules = ["openbot_pi"] if provider == "pi" else []
    settings.openai_api_key = "test-reflection-key"  # never sent to worker
    services, box = await configure(settings, path, monkeypatch)
    services.actors = None  # exercise Runner directly; scheduler has separate regressions
    async with services.session_factory() as session:
        bot = bot_actor("eng", provider=provider, model="", tool_names=["load_skill", "scratchpad"],
                        skill_names=["worker-skill"], load_repository_instructions=True)
        session.add(bot)
        await session.flush()
        session.add(BotScratchpad(actor_id=bot.id, content="GLOBAL-SCRATCHPAD-MUST-STAY-OUT"))
        await session.commit()
        thread = await create_thread(services, session, title="CLI", handles=["eng"], created_by=await human_actor(session))
    return services, box, bot, thread


async def queue(services, box, bot, thread, content):
    async with services.session_factory() as session:
        row = await session.get(Sandbox, box.id)
        ns = memory.bot_namespace(bot.id) if row.share_global_memory else memory.sandbox_namespace(bot.id, box.id)
        result = await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content=content)
        run = Run(actor_id=bot.id, thread_id=thread.id, sandbox_id=box.id, sandbox_generation=row.generation,
                  memory_namespace=list(ns))
        session.add(run)
        await session.flush()
        for item in result.items:
            if item.actor_id == bot.id:
                item.run_id, item.status = run.id, "processing"
        await session.commit()
    return run


def calls(root):
    return [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]


async def test_native_adapter_receives_explicit_environment_credential(settings, tmp_path, monkeypatch, backend):
    from openbot.db.models import SandboxCredential, SandboxCredentialGrant
    executable = tmp_path / "fake-cli"
    fake_cli(executable, tmp_path / "home" / "workspace", backend, {"EXA_API_KEY": "fake-granted"})
    plugin = "openbot_pi" if backend == "pi" else "openbot.builtin_plugins.claude"
    async with worker_at(tmp_path, leased=False, plugins=[plugin], cli_paths={backend: str(executable)}) as (_, root, path):
        services, box, bot, thread = await setup(settings, path, monkeypatch, backend)
        async with services.session_factory() as session:
            session.add(bot)
            bot.bot.skill_names = []
            credential = SandboxCredential(name="Exa", kind="env", target="EXA_API_KEY",
                                           encrypted_value=services.secrets.encrypt("fake-granted"))
            session.add(credential)
            await session.flush()
            session.add(SandboxCredentialGrant(sandbox_id=box.id, credential_id=credential.id))
            await session.commit()
        monkeypatch.setenv("EXA_API_KEY", "wrong-controller-value")
        try:
            run = await queue(services, box, bot, thread, "use app")
            await services.runner.execute(run.id)
            async with services.session_factory() as session:
                completed = await session.get(Run, run.id)
                assert completed.status == "completed", completed.error
            assert "EXA_API_KEY" in calls(root)[0]["env_keys"]
            assert "fake-granted" not in calls(root)[0]["system"]
        finally:
            await services.http_client.aclose()


async def test_worker_cli_streams_resumes_reads_project_and_scopes_reflection(settings, tmp_path, monkeypatch, backend):
    executable = tmp_path / "fake-cli"
    fake_cli(executable, tmp_path / "home" / "workspace", backend)
    plugin = "openbot_pi" if backend == "pi" else "openbot.builtin_plugins.claude"
    async with worker_at(tmp_path, leased=False, plugins=[plugin], cli_paths={backend: str(executable)}) as (_, root, path):
        (root / "AGENTS.md").write_text("WORKER-REPOSITORY-INSTRUCTIONS")
        skill = root / "skills" / "worker-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: worker-skill\ndescription: WORKER-SKILL-DESCRIPTION\n---\nworker skill body")
        services, box, bot, thread = await setup(settings, path, monkeypatch, backend)
        reflected = []
        monkeypatch.setattr(services.reflector, "schedule", lambda *a, **kw: reflected.append(kw))
        await services.store.aput(memory.bot_namespace(bot.id), "global", {"content": "GLOBAL-MEMORY-MUST-STAY-OUT"})
        for key in ("OPENBOT_API_KEY", "DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY"):
            monkeypatch.setenv(key, "controller-test-value-never-forward")

        def forbidden(*args, **kwargs):
            raise AssertionError("controller ran a CLI probe/execution or read project instructions")
        for name in ("find_executable", "child_env", "run_turn", "login_status"):
            monkeypatch.setattr(f"openbot.runtime.cli_agents.run.{name}", forbidden)
        monkeypatch.setattr("openbot.runtime.prompt.load_repository_instructions", forbidden)
        try:
            first = await queue(services, box, bot, thread, "first turn")
            await services.runner.execute(first.id)
            second = await queue(services, box, bot, thread, "second turn")
            await services.runner.execute(second.id)
            async with services.session_factory() as session:
                for run in (first, second):
                    completed = await session.get(Run, run.id)
                    assert completed.status == "completed", completed.error
                    events = (await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all()
                    assert {"cli_session", "text", "tool_call", "tool_result", "message"} <= {e.type for e in events}
                    sessions = [e.payload for e in events if e.type == "cli_session"]
                    assert all(e["scope"]["sandbox_id"] == box.id and e["scope"]["generation"] == 1 for e in sessions)
            records = calls(root)
            assert len(records) == 2 and records[0]["resumed"] is None and records[1]["resumed"] == SESSION
            assert all(r["cwd"] == str(root) and r["home"] == str(root.parent) and r["mode"] == 0o600 for r in records)
            assert all(Path(r["temporary"]).is_relative_to(root.parent / ".openbot-tmp") for r in records)
            assert all(not Path(r["temporary"]).exists() for r in records)
            for record in records:
                assert "WORKER-REPOSITORY-INSTRUCTIONS" in record["system"]
                assert "WORKER-SKILL-DESCRIPTION" in record["system"]
                assert "GLOBAL-MEMORY-MUST-STAY-OUT" not in record["system"]
                assert "GLOBAL-SCRATCHPAD-MUST-STAY-OUT" not in record["system"]
                assert not {"OPENBOT_API_KEY", "DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EXA_API_KEY"} & set(record["env_keys"])
            assert reflected and all(r["namespace"] == memory.sandbox_namespace(bot.id, box.id) for r in reflected)
            assert (root / "effects").read_text() == "once\nonce\n"
        finally:
            await services.http_client.aclose()


@pytest.mark.parametrize("own_timeout", [None, 1])
async def test_worker_cli_honors_global_and_bot_timeout_without_quarantine(settings, tmp_path, monkeypatch, backend, own_timeout):
    executable = tmp_path / "fake-cli"
    fake_cli(executable, tmp_path / "home" / "workspace", backend)
    plugin = "openbot_pi" if backend == "pi" else "openbot.builtin_plugins.claude"
    settings.cli_agent_timeout = 1 if own_timeout is None else 60
    async with worker_at(tmp_path, leased=False, plugins=[plugin], cli_paths={backend: str(executable)}) as (_, root, path):
        services, box, bot, thread = await setup(settings, path, monkeypatch, backend)
        try:
            async with services.session_factory() as session:
                from openbot.db.models import Actor
                row = await session.get(Actor, bot.id)
                row.bot.model_settings = {"timeout_seconds": own_timeout} if own_timeout else {}
                await session.commit()
            run = await queue(services, box, bot, thread, "WAIT-FOREVER")
            await asyncio.wait_for(services.runner.execute(run.id), 20)
            async with services.session_factory() as session:
                row = await session.get(Run, run.id)
                assert row.status == "failed" and "within 1s" in row.error
                assert (await session.get(Sandbox, box.id)).state == "active"
                assert {e.state for e in (await session.scalars(select(SandboxExecution))).all()} == {"completed"}
            assert (root / "effects").read_text() == "once\n"
            assert not list((root.parent / ".openbot-tmp").iterdir())
        finally:
            await services.http_client.aclose()


async def test_worker_cli_cancellation_keeps_stream_and_never_replays(settings, tmp_path, monkeypatch, backend):
    executable = tmp_path / "fake-cli"
    fake_cli(executable, tmp_path / "home" / "workspace", backend)
    plugin = "openbot_pi" if backend == "pi" else "openbot.builtin_plugins.claude"
    async with worker_at(tmp_path, leased=False, plugins=[plugin], cli_paths={backend: str(executable)}) as (_, root, path):
        services, box, bot, thread = await setup(settings, path, monkeypatch, backend)
        streamed = asyncio.Event()
        publish = services.bus.publish

        async def observe(kind, thread_id, data):
            if kind == "run.event" and data.get("type") == "text_delta":
                streamed.set()
            await publish(kind, thread_id, data)
        monkeypatch.setattr(services.bus, "publish", observe)
        run = await queue(services, box, bot, thread, "WAIT-FOREVER")
        task = asyncio.create_task(services.runner.execute(run.id))
        try:
            await asyncio.wait_for(streamed.wait(), 20)
            assert not task.done()  # event reached controller BEFORE terminal reply
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            from openbot.db.models import Message
            async with services.session_factory() as session:
                assert (await session.get(Run, run.id)).status == "cancelled"
                assert (await session.get(Sandbox, box.id)).state == "quarantined"
                intent = await session.scalar(select(SandboxExecution).where(SandboxExecution.operation == "cli_turn"))
                assert intent.state == "unknown"
                partial = await session.scalar(select(Message).where(Message.run_id == run.id, Message.sender_kind == "bot"))
                assert "worker streaming" in partial.content and partial.meta["interrupted"]
            await services.runner.execute(run.id)  # terminal run cannot be dispatched again
            assert (root / "effects").read_text() == "once\n"
            assert not list((root.parent / ".openbot-tmp").iterdir())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await services.http_client.aclose()


@pytest.mark.parametrize("mismatch", ["sandbox", "generation", "cwd", "backend", "memory", "bot", "thread"])
async def test_cli_session_reuse_is_fenced_by_every_scope_component(settings, tmp_path, monkeypatch, mismatch):
    from openbot.db.models import Thread
    backend = "claude-code"  # core adapter always installed; the scope gate is shared by every adapter
    executable = tmp_path / "fake-cli"
    fake_cli(executable, tmp_path / "home" / "workspace", backend)
    async with worker_at(tmp_path, leased=False, plugins=["openbot.builtin_plugins.claude"],
                         cli_paths={backend: str(executable)}) as (_, root, path):
        services, box, bot, thread = await setup(settings, path, monkeypatch, backend)
        monkeypatch.setattr(services.reflector, "schedule", lambda *a, **kw: None)
        try:
            scope = {"sandbox_id": box.id, "generation": box.generation,
                     "memory_namespace": list(memory.sandbox_namespace(bot.id, box.id))}
            previous = {"agent": backend, "session_id": SESSION, "cwd": str(root), "scope": scope}
            if mismatch == "sandbox":
                scope["sandbox_id"] = "different-sandbox"
            elif mismatch == "generation":
                scope["generation"] = 99
            elif mismatch == "cwd":
                previous["cwd"] = str(root / "elsewhere")
            elif mismatch == "backend":
                previous["agent"] = "pi"
            elif mismatch == "memory":
                scope["memory_namespace"] = list(memory.bot_namespace(bot.id))
            async with services.session_factory() as session:
                other_bot = bot_actor("other")
                other_thread = Thread(title="other", sandbox_id=box.id)
                session.add_all([other_bot, other_thread])
                await session.flush()
                old = Run(actor_id=other_bot.id if mismatch == "bot" else bot.id,
                          thread_id=other_thread.id if mismatch == "thread" else thread.id,
                          sandbox_id=box.id, sandbox_generation=box.generation, status="completed")
                session.add(old)
                await session.flush()
                session.add(RunEvent(run_id=old.id, seq=0, type="cli_session", payload=previous))
                await session.commit()
            run = await queue(services, box, bot, thread, "new turn, do not import a foreign session")
            await services.runner.execute(run.id)
            async with services.session_factory() as session:
                row = await session.get(Run, run.id)
                assert row.status == "completed", row.error
            assert calls(root)[0]["resumed"] is None
        finally:
            await services.http_client.aclose()
