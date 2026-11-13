"""Disposable same-UID/fake-cgroup tests, not a separate-UID maintenance pilot."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from openbot.db.models import (
    Actor,
    InboxItem,
    Run,
    Sandbox,
    SandboxCredential,
    SandboxCredentialGrant,
    SandboxExecution,
)
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.sandboxes import home, protocol, runtime
from openbot.sandboxes.maintenance import clear_home
from tests.factories import bot_actor
from tests.fakes import ai, call
from tests.test_sandboxes import configure, payload, worker_at

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux worker/home operations")


async def test_env_and_file_credentials_reach_only_isolated_children(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        async with services.session_factory() as session:
            for name, kind, target, value in (("Exa", "env", "EXA_API_KEY", "fake-env"), ("SSH", "file", ".ssh/test_key", "fake-file")):
                row = SandboxCredential(name=name, kind=kind, target=target, encrypted_value=services.secrets.encrypt(value))
                session.add(row)
                await session.flush()
                session.add(SandboxCredentialGrant(sandbox_id=box.id, credential_id=row.id))
            await session.commit()
        monkeypatch.setenv("EXA_API_KEY", "supervisor-only-fake")
        monkeypatch.setenv("OPENBOT_API_KEY", "controller-only-fake")
        spawn = asyncio.create_subprocess_exec
        launches = []
        async def checked_spawn(*args, **kwargs):
            if "--execute" in args:
                assert "EXA_API_KEY" not in kwargs["env"] and "OPENBOT_API_KEY" not in kwargs["env"]
                launches.append(args)
            return await spawn(*args, **kwargs)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_spawn)
        run = SimpleNamespace(id="run", sandbox_id=box.id, sandbox_generation=box.generation)
        thread = SimpleNamespace(sandbox_id=box.id, working_directory=None)
        try:
            async with runtime.binding(services, run, thread):
                result = await runtime.execute(services, box.id, run.id, "run_shell", {"command": 'printf "%s|%s" "$EXA_API_KEY" "${OPENBOT_API_KEY-unset}"'}, None)
                assert "fake-env|unset" in result
            assert launches
            assert os.environ["EXA_API_KEY"] == "supervisor-only-fake"
            key = root.parent / ".ssh/test_key"
            assert key.read_text() == "fake-file"
            assert key.stat().st_mode & 0o777 == 0o600
            async with services.session_factory() as session:
                assert {x.operation for x in (await session.scalars(select(SandboxExecution))).all()} == {"provision", "probe", "run_shell"}
        finally:
            await services.http_client.aclose()


def test_file_provision_refuses_symlinks_hardlinks_and_mounts(tmp_path, monkeypatch):
    private = tmp_path / "home"
    private.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("original")
    monkeypatch.setattr(home, "mountpoints", lambda: set())
    (private / ".ssh").symlink_to(outside, target_is_directory=True)
    with pytest.raises(protocol.SandboxRejected):
        home.provision(private, {".ssh/secret": "replacement"})
    assert (outside / "secret").read_text() == "original"
    (private / ".ssh").unlink()
    (private / ".ssh").mkdir()
    os.link(outside / "secret", private / ".ssh/key")
    with pytest.raises(protocol.SandboxRejected):
        home.provision(private, {".ssh/key": "replacement"})
    monkeypatch.setattr(home, "mountpoints", lambda: {private / ".ssh"})
    with pytest.raises(protocol.SandboxRejected, match="mount"):
        home.provision(private, {".ssh/new": "replacement"})


def test_clear_excludes_mounts_symlink_targets_and_top_level_dotfiles(tmp_path):
    private = tmp_path / "home"
    root = private / "workspace"
    mounted = root / "shared"
    mounted.mkdir(parents=True)
    (mounted / "keep").write_text("mount data")
    (root / ".nested-hidden").write_text("workspace hidden files are not top-level home dotfiles")
    (private / ".login").write_text("fake-login")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("unrelated")
    (private / "link").symlink_to(outside, target_is_directory=True)
    result = home.clear(private, root, True, mounts={mounted})
    assert ".login" in result["kept"] and "workspace/shared" in result["kept"]
    assert (private / ".login").read_text() == "fake-login"
    assert not (root / ".nested-hidden").exists()
    assert not (private / "link").exists()
    assert (outside / "keep").exists() and (mounted / "keep").exists()
    home.clear(private, root, False, mounts={mounted})
    assert not (private / ".login").exists()
    assert (mounted / "keep").exists()
    assert root.is_dir() and (private / ".openbot-tmp").is_dir()


@pytest.mark.parametrize("keep", [True, False])
async def test_operator_home_clear_is_durable_and_fences_continuation(settings, tmp_path, monkeypatch, keep):
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        (root / "delete").write_text("disposable")
        (root.parent / ".login").write_text("fake-login")
        try:
            result = await clear_home(services, box.id, keep_dotfiles=keep, confirmation=box.name)
            assert result["keep_dotfiles"] is keep
            assert not (root / "delete").exists()
            assert (root.parent / ".login").exists() is keep
            async with services.session_factory() as session:
                current = await session.get(Sandbox, box.id)
                assert current.state == "active" and current.generation == box.generation + 1
                item = await session.scalar(select(SandboxExecution))
                assert item.state == "completed"
                run = SimpleNamespace(id="old", sandbox_id=box.id, sandbox_generation=box.generation)
                thread = SimpleNamespace(sandbox_id=box.id, working_directory=None)
            with pytest.raises(protocol.SandboxRejected, match="stale"):
                async with runtime.binding(services, run, thread):
                    pytest.fail("old run must not resume")
            run.sandbox_generation += 1
            async with runtime.binding(services, run, thread):
                assert "after" in await runtime.execute(services, box.id, run.id, "run_shell", {"command": "echo after"}, None)
        finally:
            await services.http_client.aclose()


async def test_stale_clear_request_and_lost_result_never_replay(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        (root / "before").write_text("disposable")
        try:
            await clear_home(services, box.id, keep_dotfiles=True, confirmation=box.name, generation=1)
            (root / "new-work").write_text("must survive a stale request")
            with pytest.raises(protocol.SandboxRejected, match="generation"):
                await clear_home(services, box.id, keep_dotfiles=True, confirmation=box.name, generation=1)
            assert (root / "new-work").exists()
            request = protocol.request
            async def lose_reply(*args, **kwargs):
                await request(*args, **kwargs)
                raise protocol.SandboxError("injected lost reply after actual deletion")
            monkeypatch.setattr(protocol, "request", lose_reply)
            with pytest.raises(protocol.SandboxError):
                await clear_home(services, box.id, keep_dotfiles=True, confirmation=box.name, generation=2)
            assert not (root / "new-work").exists()
            async with services.session_factory() as session:
                assert (await session.get(Sandbox, box.id)).state == "quarantined"
            with pytest.raises(protocol.SandboxRejected):
                await clear_home(services, box.id, keep_dotfiles=True, confirmation=box.name, generation=3)
        finally:
            await services.http_client.aclose()


async def test_clear_wrong_confirmation_and_active_worker_do_not_delete(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        (root / "keep").write_text("safe")
        try:
            with pytest.raises(protocol.SandboxRejected, match="confirm"):
                await clear_home(services, box.id, keep_dotfiles=False, confirmation="wrong")
            with pytest.raises(protocol.SandboxRejected, match="idle"):
                await protocol.request(path, os.getuid(), payload("clear_home", keep_dotfiles=False))
            assert (root / "keep").exists()
        finally:
            await services.http_client.aclose()


async def test_unconfirmed_home_cleanup_quarantines_and_is_not_repeated(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        services, box = await configure(settings, path, monkeypatch)
        (root / "keep").write_text("safe")
        async def fail():
            raise OSError("fake cgroup cleanup failed")
        monkeypatch.setattr(worker.groups, "quiesce", fail)
        try:
            with pytest.raises(protocol.SandboxError):
                await clear_home(services, box.id, keep_dotfiles=False, confirmation=box.name)
            assert (root / "keep").exists() and worker.maintenance
            async with services.session_factory() as session:
                assert (await session.get(Sandbox, box.id)).state == "quarantined"
                assert (await session.scalar(select(SandboxExecution))).state == "unknown"
            with pytest.raises(protocol.SandboxRejected):
                await clear_home(services, box.id, keep_dotfiles=False, confirmation=box.name)
        finally:
            await services.http_client.aclose()


async def test_clear_cancels_active_and_queued_work_before_deleting(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        services, box = await configure(settings, path, monkeypatch, {"eng": [
            ai(tool_calls=[call("run_shell", command="touch started; sleep 30; touch must-not-survive")]), ai("done")],
            "waiter": [ai("must not execute")]})
        async with services.session_factory() as session:
            session.add_all([bot_actor("eng", tool_names=["run_shell"]), bot_actor("waiter")])
            await session.commit()
            thread = await create_thread(services, session, title="reset-test", handles=["eng", "waiter"],
                                         default_bot_handle="eng", created_by=await human_actor(session))
        await services.actors.start()
        try:
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="go")
            async with asyncio.timeout(15):
                while not (root / "started").exists():
                    await asyncio.sleep(.02)
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="@waiter wait")
            await clear_home(services, box.id, keep_dotfiles=True, confirmation=box.name)
            await services.actors.wait_idle(timeout=10)
            assert not worker.operations and worker.active_run is None
            assert not (root / "started").exists() and not (root / "must-not-survive").exists()
            async with services.session_factory() as session:
                assert (await session.get(Sandbox, box.id)).state == "active"
                runs = (await session.scalars(select(Run))).all()
                assert all(run.status in ("cancelled", "failed") for run in runs)
                items = (await session.scalars(select(InboxItem).where(
                    InboxItem.status.in_(("queued", "processing")),
                    InboxItem.actor_id.in_(select(Actor.id).where(Actor.kind == "bot"))))).all()
                assert not items
                effects = (await session.scalars(select(SandboxExecution).where(SandboxExecution.operation == "run_shell"))).all()
                assert effects and effects[0].state == "unknown"  # cleanup does not undo external effects
        finally:
            await services.actors.stop()
            await services.http_client.aclose()


async def test_worker_proves_quiescence_before_clearing(tmp_path):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        visited = []
        async def quiesce():
            assert (root / "before").exists()
            visited.append(True)
            await asyncio.sleep(.01)
        worker.groups.quiesce = quiesce
        (root / "before").write_text("test")
        await protocol.request(path, os.getuid(), payload("clear_home", keep_dotfiles=False))
        assert visited == [True] and not (root / "before").exists()
