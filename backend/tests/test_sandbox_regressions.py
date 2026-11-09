"""Regression checks use same-UID sockets/fake cgroups, NOT an OS-isolation claim."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from openbot.db.models import Run, Sandbox, SandboxExecution
from openbot.runtime.delivery import create_thread, human_actor, post_message
from openbot.sandboxes import protocol, runtime
from tests.factories import bot_actor
from tests.fakes import ai, call
from tests.sandbox_fakes import fake_lease
from tests.test_sandbox_lifecycle import seed
from tests.test_sandboxes import configure, payload, worker_at

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux worker transport")


async def active(services, ident):
    async with services.session_factory() as session:
        box = await session.get(Sandbox, ident)
        assert box.state == "active"
        return box


async def test_missing_directory_is_rejected_then_binding_can_be_used(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch)
        bot, thread, run = await seed(services, box)
        thread.working_directory = "not-yet-created"
        try:
            with pytest.raises(protocol.SandboxRejected, match="working_directory"):
                async with runtime.binding(services, run, thread, bot):
                    pytest.fail("preflight should reject missing cwd")
            assert (await active(services, box.id)).active_run_id is None
            async with services.session_factory() as session:
                assert (await session.scalar(select(SandboxExecution))).state == "rejected"
            (root / "not-yet-created").mkdir()
            async with runtime.binding(services, run, thread, bot):
                result = await runtime.execute(services, box.id, run.id, "run_shell", {"command": "pwd"}, thread.working_directory)
                assert str(root / "not-yet-created") in result
            await active(services, box.id)
        finally:
            await services.http_client.aclose()


async def test_ordinary_http_and_file_errors_leave_parallel_run_usable(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (_, root, path):
        services, box = await configure(settings, path, monkeypatch, {"eng": [
            ai(tool_calls=[call("fetch_url", url="ftp://example.test"), call("read_file", path="missing")]),
            ai(tool_calls=[call("write_file", path="after-errors", content="usable")]), ai("Done"),
        ]})
        async with services.session_factory() as session:
            session.add(bot_actor("eng", tool_names=["fetch_url", "read_file", "write_file"]))
            await session.commit()
            thread = await create_thread(services, session, title="errors", handles=["eng"], created_by=await human_actor(session))
        await services.actors.start()
        try:
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=thread.id, sender=await human_actor(session), content="go")
            await services.actors.wait_idle(timeout=30)
            async with services.session_factory() as session:
                run = await session.scalar(select(Run).where(Run.thread_id == thread.id))
                assert run.status == "completed", run.error
                assert {x.state for x in (await session.scalars(select(SandboxExecution))).all()} == {"completed"}
            assert (root / "after-errors").read_text() == "usable"
            await active(services, box.id)
        finally:
            await services.actors.stop()
            await services.http_client.aclose()


async def test_parallel_shell_barrier_and_independent_cancellation(tmp_path):
    async with worker_at(tmp_path) as (worker, root, path):
        # Neither can finish unless both are executing, not queued behind one lock.
        async def shell(command):
            return await protocol.request(path, os.getuid(), payload("run_shell", {"command": command, "timeout": 10}))
        a, b = await asyncio.gather(
            shell("touch a; while [ ! -e b ]; do sleep .02; done; echo both-a"),
            shell("touch b; while [ ! -e a ]; do sleep .02; done; echo both-b"))
        assert "both-a" in a and "both-b" in b
        one = asyncio.create_task(shell("touch cancel-started; sleep 30; touch forbidden"))
        two = asyncio.create_task(shell("touch sibling-started; while [ ! -e release ]; do sleep .02; done; echo sibling-ok"))
        try:
            async with asyncio.timeout(10):
                while not all((root / name).exists() for name in ("cancel-started", "sibling-started")):
                    await asyncio.sleep(.02)
            assert len(worker.operations) == 2
            one.cancel()
            with pytest.raises(asyncio.CancelledError):
                await one
            async with asyncio.timeout(5):
                while len(worker.operations) != 1:
                    await asyncio.sleep(.02)
            assert not two.done()
            (root / "release").touch()
            assert "sibling-ok" in await two
            assert not (root / "forbidden").exists()
        finally:
            for task in (one, two):
                task.cancel()
            await asyncio.gather(one, two, return_exceptions=True)


async def test_tool_reuse_preserves_large_timeout_and_output_spilling(tmp_path, monkeypatch):
    from openbot.sandboxes.operations import perform
    from openbot.sandboxes.policy import WORKER_TOOLS
    seen = []

    async def fake_shell(command, timeout, runtime, **kwargs):
        seen.append((command, timeout, runtime.context.shell_login))
        return "original tool result"

    monkeypatch.setitem(WORKER_TOOLS, "run_shell", WORKER_TOOLS["run_shell"].model_copy(update={"coroutine": fake_shell}))

    async def emit(*args):
        pass

    assert await perform(tmp_path, payload("run_shell", {"command": "long-job", "timeout": 3600}), {}, emit) == "original tool result"
    assert seen == [("long-job", 3600, False)]
    async with worker_at(tmp_path) as (_, root, path):
        result = await protocol.request(path, os.getuid(), payload("run_shell",
            {"command": "printf '%10000s' x"}, shell_output_cap=200))
        assert ".openbot/tool-output/" in result
        saved = list((root / ".openbot" / "tool-output").iterdir())
        assert saved and len(saved[0].read_text()) >= 10000


async def test_busy_a_waiter_does_not_take_idle_b_global_permit(settings, tmp_path, monkeypatch):
    settings.max_concurrent_runs = 2
    services, box_a = await configure(settings, str(tmp_path / "unused-a"), monkeypatch)
    monkeypatch.setattr(protocol, "run_lease", fake_lease)

    async def response(*args, **kwargs):
        return {"cwd": "/worker"}
    monkeypatch.setattr(protocol, "request", response)
    second_slot = settings.sandbox_resources["slot"].model_copy(update={"socket_path": str(tmp_path / "unused-b"), "uid": os.getuid() + 1})
    settings.sandbox_resources["other"] = second_slot
    async with services.session_factory() as session:
        box_b = Sandbox(name="other", resource="other", resource_key="other", resource_spec=second_slot.model_dump())
        session.add_all([box_b, *(bot_actor(name) for name in ("a1", "a2", "b1"))])
        await session.commit()
        threads = {}
        for name, box in (("a1", box_a), ("a2", box_a), ("b1", box_b)):
            threads[name] = await create_thread(services, session, title=name, handles=[name],
                sandbox_id=box.id, created_by=await human_actor(session))
    started = {name: asyncio.Event() for name in threads}
    release = asyncio.Event()
    active_runs = set()
    max_active = 0

    async def execute(run_id, resume):
        nonlocal max_active
        async with services.session_factory() as session:
            run = await session.get(Run, run_id)
        name = next(name for name, thread in threads.items() if thread.id == run.thread_id)
        assert not any(sandbox_id == run.sandbox_id for _, sandbox_id in active_runs)
        active_runs.add((name, run.sandbox_id))
        max_active = max(max_active, len(active_runs))
        started[name].set()
        await release.wait()
        active_runs.remove((name, run.sandbox_id))
        await services.runner._set_status(run.id, "completed")

    monkeypatch.setattr(services.runner, "_execute", execute)
    await services.actors.start()
    try:
        async def post(name):
            async with services.session_factory() as session:
                await post_message(services, session, thread_id=threads[name].id,
                                   sender=await human_actor(session), content="go")
        await post("a1")
        await asyncio.wait_for(started["a1"].wait(), 5)
        await post("a2")
        # Wait for A2's queued Run: it is now waiting for A, without a global permit.
        async with asyncio.timeout(5):
            while True:
                async with services.session_factory() as session:
                    if await session.scalar(select(Run.id).where(Run.thread_id == threads["a2"].id)):
                        break
                await asyncio.sleep(.01)
        await post("b1")
        await asyncio.wait_for(started["b1"].wait(), 5)
        assert not started["a2"].is_set()
        assert max_active == 2
        release.set()
        await services.actors.wait_idle(timeout=10)
        assert started["a2"].is_set()
    finally:
        release.set()
        await services.actors.stop()
        await services.http_client.aclose()


async def test_missing_cleanup_prerequisite_is_preflight_rejection(tmp_path, monkeypatch):
    from openbot.sandboxes import isolation

    def unavailable():
        raise PermissionError("user namespaces disabled by administrator")
    monkeypatch.setattr(isolation, "_user_mount_namespace", unavailable)
    # Inspect no host mounts; provide the expected public kernel metadata as a fixture.
    monkeypatch.setattr(isolation.Path, "read_text", lambda *_a, **_kw: "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw")
    with pytest.raises(protocol.SandboxRejected, match="no process-group fallback"):
        isolation.lock_cgroup_mount()


async def test_namespace_setup_locks_readonly_mount_in_second_user_namespace(monkeypatch):
    from openbot.sandboxes import isolation
    sequence = []
    monkeypatch.setattr(isolation.Path, "read_text", lambda *_a, **_kw: "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw")
    monkeypatch.setattr(isolation, "_user_mount_namespace", lambda: sequence.append("namespace"))

    def mount(source, target, fs, flags, data):
        sequence.append((target, flags.value))
        if sequence.count("namespace") == 2:
            isolation.ctypes.set_errno(1)
            return -1
        return 0
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(mount=mount))
    isolation.lock_cgroup_mount()
    assert sequence[0] == sequence[-2] == "namespace"
    assert sequence[-3][1] & 1  # read-only BEFORE inheriting the mount into the inner namespace
    assert not sequence[-1][1] & 1  # remount-rw must be denied before running any agent code


async def test_unlocked_cgroup_mount_is_refused(monkeypatch):
    from openbot.sandboxes import isolation
    monkeypatch.setattr(isolation.Path, "read_text", lambda *_a, **_kw: "1 0 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw")
    monkeypatch.setattr(isolation, "_user_mount_namespace", lambda: None)
    monkeypatch.setattr(isolation.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace(mount=lambda *a: 0))
    with pytest.raises(protocol.SandboxRejected, match="not kernel-locked"):
        isolation.lock_cgroup_mount()


async def test_controller_lease_loss_stops_run_children(tmp_path):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        reader, writer = await asyncio.open_unix_connection(path)
        await protocol.send(writer, {**payload("run"), "version": protocol.VERSION})
        assert (await protocol.receive(reader))["type"] == "ready"
        operation = asyncio.create_task(protocol.request(path, os.getuid(), payload("run_shell", {
            "command": "echo once > started; sleep 30; touch after-controller-loss"})))
        async with asyncio.timeout(10):
            while not (root / "started").exists():
                await asyncio.sleep(.02)
        writer.close()  # no finish frame: emulate a dead controller
        await writer.wait_closed()
        with pytest.raises(protocol.SandboxError):
            await operation
        async with asyncio.timeout(10):
            while worker.active_run is not None:
                await asyncio.sleep(.02)
        assert not worker.operations and not (root / "after-controller-loss").exists()


async def test_unconfirmed_run_cleanup_quarantines_even_after_success(settings, tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        services, box = await configure(settings, path, monkeypatch)
        bot, thread, run = await seed(services, box)
        end = worker.groups.end

        async def failed_confirmation(run_id):
            await end(run_id)  # clean real test subprocesses, then emulate missing confirmation
            raise OSError("cleanup confirmation lost")
        monkeypatch.setattr(worker.groups, "end", failed_confirmation)
        try:
            with pytest.raises(protocol.SandboxError, match="quarantined"):
                async with runtime.binding(services, run, thread, bot):
                    await runtime.execute(services, box.id, run.id, "write_file", {"path": "effect", "content": "once"}, None)
            assert (root / "effect").read_text() == "once"
            async with services.session_factory() as session:
                assert (await session.get(Sandbox, box.id)).state == "quarantined"
            assert worker.active_run == run.id  # worker also refuses another lease
        finally:
            await services.http_client.aclose()


async def test_run_finish_fences_new_operations_and_does_not_interrupt_cleanup(tmp_path, monkeypatch):
    async with worker_at(tmp_path, leased=False) as (worker, root, path):
        reader, writer = await asyncio.open_unix_connection(path)
        await protocol.send(writer, {**payload("run"), "version": protocol.VERSION})
        assert (await protocol.receive(reader))["type"] == "ready"
        request = payload("run_shell", {"command": "touch started; sleep 30"})
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        kill = worker.groups.kill

        async def slow_cleanup(group):
            if group.name == request["id"]:
                entered.set()
                await release.wait()
                await kill(group)
                cleaned.set()
            else:
                await kill(group)
        monkeypatch.setattr(worker.groups, "kill", slow_cleanup)
        task = asyncio.create_task(protocol.request(path, os.getuid(), request))
        try:
            async with asyncio.timeout(10):
                while not (root / "started").exists():
                    await asyncio.sleep(.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(entered.wait(), 5)
            await protocol.send(writer, {"finish": True})
            async with asyncio.timeout(5):
                while not worker.closing_run:
                    await asyncio.sleep(.01)
            with pytest.raises(protocol.SandboxRejected, match="lease"):
                await protocol.request(path, os.getuid(), payload("probe"))
            assert not cleaned.is_set()
            release.set()
            assert (await protocol.receive(reader))["result"] == "clean"
            assert cleaned.is_set() and worker.active_run is None
            assert not list((root.parent / ".openbot-tmp").iterdir())
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            writer.close()
            await writer.wait_closed()


async def test_cancellation_after_claim_commit_releases_admission(settings, tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession
    services, box = await configure(settings, str(tmp_path / "unused"), monkeypatch)
    bot, thread, run = await seed(services, box)
    commit = AsyncSession.commit
    first = True

    async def cancelled_commit(session):
        nonlocal first
        await commit(session)
        if first:
            first = False
            raise asyncio.CancelledError
    monkeypatch.setattr(AsyncSession, "commit", cancelled_commit)
    try:
        with pytest.raises(asyncio.CancelledError):
            async with runtime.binding(services, run, thread, bot):
                pytest.fail("cancelled before worker admission")
        assert (await active(services, box.id)).active_run_id is None
    finally:
        await services.http_client.aclose()
