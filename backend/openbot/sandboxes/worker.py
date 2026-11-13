"""Preprovisioned Linux worker with a run lease and concurrent operation cgroups."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import struct
import sys
from contextlib import suppress
from pathlib import Path

from openbot.sandboxes.protocol import (
    VERSION,
    SandboxError,
    SandboxRejected,
    peer_uid,
    receive,
    send,
)

ENV_NAMES = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE")
OPERATIONS = {"probe", "prepare", "browse", "skills", "cli_probe", "cli_turn", "provision"}


def environment(home: str) -> dict[str, str]:
    import pwd
    env = {k: os.environ[k] for k in ENV_NAMES if k in os.environ}
    env.update(HOME=home, USER=pwd.getpwuid(os.getuid()).pw_name, LOGNAME=pwd.getpwuid(os.getuid()).pw_name,
               TMPDIR=str(Path(home) / ".openbot-tmp"))
    return env


class Worker:
    def __init__(self, resource, root, home, controller_uid, groups, *, plugins=(), cli_paths=None,
                 _test_without_namespaces=False):
        self.resource, self.root, self.home = resource, root, home
        self.controller_uid, self.groups = controller_uid, groups
        self.config = {"plugins": list(plugins), "cli_paths": cli_paths or {},
                       "isolate": not _test_without_namespaces}
        self.active_run = None
        self.closing_run = False
        self.maintenance = False
        self.operations = {}  # operation ID -> task; no global operation semaphore
        self.cleaning = set()
        self.seen = set()

    async def _reply(self, writer, ident, message_type, **values):
        await send(writer, {"version": VERSION, "id": ident, "type": message_type, **values})

    async def _run(self, payload, reader, writer):
        run_id = payload.get("run_id")
        if self.active_run is not None or self.maintenance:
            raise SandboxRejected("worker already has an active run or maintenance operation")
        self.groups.begin(run_id)
        self.active_run = run_id
        self.closing_run = False
        self.seen.clear()
        try:
            await self._reply(writer, payload["id"], "ready")
            # Either explicit finish or EOF terminates the run, including controller crash.
            try:
                finish = await receive(reader)
            except asyncio.IncompleteReadError:
                pass
            else:
                if finish != {"finish": True}:
                    raise SandboxError("invalid run-finish message")
        finally:
            self.closing_run = True  # fence new operations before taking the task snapshot
            tasks = list(self.operations.values())
            for ident, task in self.operations.items():
                if ident not in self.cleaning:
                    task.cancel()  # do not interrupt an operation already proving cleanup
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.groups.end(run_id)
            for ident in self.operations:  # any operation whose individual cleanup failed
                with suppress(FileNotFoundError):
                    shutil.rmtree(self.home / ".openbot-tmp" / ident)
            self.operations.clear()
            self.cleaning.clear()
            self.active_run = None  # only after verified whole-run cleanup
        await self._reply(writer, payload["id"], "result", result="clean")

    async def _operation(self, payload, reader, writer):
        from openbot.sandboxes.policy import WORKER_TOOLS
        name, ident = payload.get("operation"), payload["id"]
        if payload.get("run_id") != self.active_run or self.active_run is None or self.closing_run:
            raise SandboxRejected("operation has no active run lease")
        if name not in (*WORKER_TOOLS, *OPERATIONS):
            raise SandboxRejected(f"unsupported worker operation: {name}")
        if ident in self.seen:
            raise SandboxRejected("duplicate operation ID; inspect its recorded outcome, do not replay")
        self.seen.add(ident)
        group = self.groups.operation(self.active_run, ident)
        temporary = self.home / ".openbot-tmp" / ident
        temporary.mkdir(mode=0o700)
        env = {**environment(str(self.home)), "TMPDIR": str(temporary)}
        proc = None
        disconnected = asyncio.create_task(reader.read(1))
        stream_task = None
        deadline = None
        self.operations[ident] = asyncio.current_task()
        result = {"type": "unknown"}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "openbot.sandboxes.worker", "--execute", str(self.root),
                "--cgroup", str(group), cwd=self.home, env=env,
                start_new_session=True, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            proc.stdin.write(json.dumps({**payload, "worker_config": self.config}).encode())
            await proc.stdin.drain()
            proc.stdin.close()

            async def stream():
                nonlocal deadline
                while True:
                    packet = await receive(proc.stdout)
                    if packet.get("type") != "event":
                        return packet
                    if packet.get("kind") == "operation_started":
                        # The tool owns its requested timeout. This watchdog only
                        # handles an escaped descendant keeping its cleanup pipes open.
                        if name == "run_shell":
                            seconds = max(0, float(payload.get("args", {}).get("timeout", 120)))
                            deadline = asyncio.create_task(asyncio.sleep(seconds + 5))
                        elif name == "cli_turn":
                            from openbot.runtime.cli_agents.base import (
                                KILL_GRACE,
                                PIPE_GRACE,
                                STATUS_TIMEOUT,
                                VERSION_TIMEOUT,
                            )
                            args = payload["args"]
                            turns = 2 if args.get("resume") else 1  # explicit UnknownSession fallback only
                            grace = VERSION_TIMEOUT + STATUS_TIMEOUT + 2 * (KILL_GRACE + PIPE_GRACE) + 5
                            deadline = asyncio.create_task(asyncio.sleep(turns * float(args["timeout"]) + grace))
                        continue
                    await self._reply(writer, ident, "event", kind=packet["kind"], payload=packet.get("payload"))

            stream_task = asyncio.create_task(stream())
            while True:
                waiting = [stream_task, disconnected, *([deadline] if deadline else [])]
                done, _ = await asyncio.wait(waiting, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    raise asyncio.CancelledError
                if stream_task in done:
                    result = stream_task.result()
                    break
                if deadline and deadline in done:
                    result = ({"type": "result", "result": f"error: command timed out after {payload.get('args', {}).get('timeout', 120)}s"}
                              if name == "run_shell" else {"type": "unknown"})
                    break
        finally:
            # cgroup.kill reaches setsid/double-fork descendants; siblings have
            # different groups. Cleanup completes BEFORE a terminal response.
            self.cleaning.add(ident)
            await self.groups.kill(group)
            if proc is not None:
                async with asyncio.timeout(10):
                    await proc.wait()
            for task in (stream_task, disconnected, deadline):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (stream_task, disconnected, deadline) if t is not None),
                                 return_exceptions=True)
            shutil.rmtree(temporary)  # includes prompts left behind by killed CLI subprocesses
            self.operations.pop(ident, None)
            self.cleaning.discard(ident)
        await self._reply(writer, ident, result.pop("type"), **result)

    async def _clear_home(self, payload, writer):
        if self.active_run is not None or self.maintenance:
            raise SandboxRejected("home clearing requires an idle worker")
        if not isinstance(payload.get("keep_dotfiles"), bool):
            raise SandboxRejected("keep_dotfiles must be explicitly selected")
        self.maintenance = True
        # Stay fenced on any unknown/partial cleanup outcome. Restart/reconciliation
        # is an operator action, never a retry of destructive work.
        await self.groups.quiesce()
        from openbot.sandboxes.home import clear
        result = clear(self.home, self.root, payload["keep_dotfiles"])
        self.maintenance = False
        await self._reply(writer, payload["id"], "result", result=result)

    async def handle(self, reader, writer):
        ident = None
        try:
            if peer_uid(writer) != self.controller_uid:
                return
            async with asyncio.timeout(10):
                payload = await receive(reader)
            ident = payload.get("id")
            if (payload.get("version") != VERSION or payload.get("resource") != self.resource
                    or not isinstance(ident, str) or len(ident) != 36):
                raise SandboxRejected("invalid request/version/resource")
            if payload.get("operation") == "run":
                await self._run(payload, reader, writer)
            elif payload.get("operation") == "clear_home":
                await self._clear_home(payload, writer)
            else:
                await self._operation(payload, reader, writer)
        except SandboxRejected as exc:
            with suppress(OSError):
                await self._reply(writer, ident, "rejected", error=str(exc))
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- never expose credential-bearing internal exceptions
            with suppress(OSError):
                await self._reply(writer, ident, "unknown")
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def serve(args):
    from openbot.sandboxes.cgroups import Cgroups
    from openbot.sandboxes.isolation import protect_supervisor
    if os.getuid() == 0 or args.controller_uid in (0, os.getuid()):
        raise SystemExit("worker and controller require distinct non-root UIDs")
    home = Path.home().resolve()
    if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o077:
        raise SystemExit("worker home must be owned by its UID with mode 0700")
    root = Path(args.root).resolve(strict=True)
    if not root.is_relative_to(home):
        raise SystemExit("worker root must be inside its private home")
    (home / ".openbot-tmp").mkdir(mode=0o700, exist_ok=True)
    protect_supervisor()
    cgroup_root = args.cgroup_root
    if cgroup_root is None:
        own = next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                    if line.startswith("0::")), None)
        if not own or own == "/":
            raise SystemExit("worker needs a delegated service cgroup, not the hierarchy root")
        cgroup_root = "/sys/fs/cgroup" + own
    groups = Cgroups(Path(cgroup_root))
    worker = Worker(args.resource, root, home, args.controller_uid, groups,
                    plugins=args.plugin, cli_paths=dict(item.split("=", 1) for item in args.cli_path))
    socket_path = Path(args.socket)
    parent = socket_path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise SystemExit("socket parent must be worker-owned and not group/world writable")
    if socket_path.exists():
        raise SystemExit("socket already exists; stop unit and reconcile before restart")
    os.umask(0o007)
    server = await asyncio.start_unix_server(worker.handle, path=str(socket_path))
    os.umask(0o077)
    async with server:
        await server.serve_forever()


async def execute_child(root, payload):
    """Async pipe backpressure must not block the CLI's own timeout/cancellation loop."""
    from openbot.sandboxes.operations import perform
    loop = asyncio.get_running_loop()
    sink = asyncio.StreamReader()
    transport, flow = await loop.connect_write_pipe(lambda: asyncio.StreamReaderProtocol(sink), sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, flow, None, loop)

    async def emit(kind, value):
        await send(writer, {"type": "event", "kind": kind, "payload": value})

    try:
        try:
            # This child has already joined and locked its operation namespace.
            # Credentials must never affect supervisor/bootstrap interpreter startup.
            from openbot.sandboxes.credentials import validate_target
            for key, value in payload.get("credentials", {}).get("env", {}).items():
                validate_target("env", key)
                os.environ[key] = value
            result = await perform(root, payload, dict(os.environ), emit)
            packet = {"type": "result", "result": result}
        except SandboxRejected as exc:
            packet = {"type": "rejected", "error": str(exc)}
        except Exception:  # noqa: BLE001 -- no internal/credential-bearing exception text
            packet = {"type": "unknown"}
        await send(writer, packet)
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    if sys.platform != "linux":
        raise SystemExit("sandbox worker requires Linux")
    if len(sys.argv) == 5 and sys.argv[1] == "--execute" and sys.argv[3] == "--cgroup":
        def output(value):
            body = json.dumps(value).encode()
            sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)
            sys.stdout.buffer.flush()

        try:
            payload = json.load(sys.stdin)
            os.umask(0o077)
            # Join and protect cgroup access before importing adapters or reading project files.
            (Path(sys.argv[4]) / "cgroup.procs").write_text(str(os.getpid()))
            if payload["worker_config"].get("isolate", True):
                from openbot.sandboxes.isolation import lock_cgroup_mount
                lock_cgroup_mount()
            asyncio.run(execute_child(Path(sys.argv[2]), payload))
        except SandboxRejected as exc:
            output({"type": "rejected", "error": str(exc)})
        except Exception:  # noqa: BLE001 -- unknown failures require inspection, never replay
            output({"type": "unknown"})
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--controller-uid", required=True, type=int)
    parser.add_argument("--cgroup-root", help="delegated service cgroup; defaults to /proc/self/cgroup")
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--cli-path", action="append", default=[])
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
