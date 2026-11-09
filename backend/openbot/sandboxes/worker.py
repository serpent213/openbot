"""Preprovisioned Linux worker. Never run as root or import server configuration.

Run under a dedicated systemd UID/cgroup with a private home and administrator-
owned socket parent. No account management, privilege elevation or auto-recycling.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from openbot.sandboxes.policy import WORKER_TOOLS, directory
from openbot.sandboxes.protocol import VERSION, SandboxError, peer_uid, receive, send

OUTPUT_LIMIT = 24000
ENV_NAMES = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "NIX_SSL_CERT_FILE")


def environment(home: str) -> dict[str, str]:
    import pwd
    env = {k: os.environ[k] for k in ENV_NAMES if k in os.environ}
    env.update(HOME=home, USER=pwd.getpwuid(os.getuid()).pw_name, LOGNAME=pwd.getpwuid(os.getuid()).pw_name)
    return env


async def bounded(stream):
    result = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        available = max(0, OUTPUT_LIMIT - len(result))
        result.extend(chunk[:available])
        truncated |= len(chunk) > available
    return result.decode(errors="replace") + ("\n[truncated]" if truncated else "")


async def stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Process.wait() can wait for pipe EOF from an escaped descendant. Reaping
    # the group leader must not block quarantine; whole-cgroup cleanup is external.
    async with asyncio.timeout(5):
        while proc.returncode is None:
            await asyncio.sleep(0.01)


async def operation(root: Path, payload: dict) -> str:
    """Executed only in a disposable worker-UID child, never in the controller."""
    import httpx
    from bs4 import BeautifulSoup

    from openbot.tools.builtin.workspace import resolve_in_workspace
    from openbot.tools.context import RunContext

    cwd = resolve_in_workspace(root, directory(payload.get("directory")))
    if not cwd.is_dir():
        raise ValueError("sandbox working directory does not exist")
    name, args = payload["operation"], payload.get("args", {})
    if name == "probe":
        return "ready"
    if name not in WORKER_TOOLS or not isinstance(args, dict):
        raise ValueError("unsupported sandbox operation")
    # Validate against the same schema advertised to the model. Runtime is never supplied over RPC.
    args = WORKER_TOOLS[name].tool_call_schema.model_validate(args).model_dump()
    if name == "run_shell":
        where = resolve_in_workspace(cwd, args.get("cwd"))
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", args["command"], cwd=where,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.gather(bounded(proc.stdout), bounded(proc.stderr))
        code = await proc.wait()
        return f"exit code: {code}\nstdout:\n{out}\nstderr:\n{err}"
    if name in ("http_request", "fetch_url"):
        method = args.get("method", "GET").upper()
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False) as client:  # noqa: SIM117
            async with client.stream(method, args["url"], headers=args.get("headers"),
                                     content=args.get("body")) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk[:max(0, OUTPUT_LIMIT - len(data))])
                    if len(data) >= OUTPUT_LIMIT:
                        break
                text = data.decode(errors="replace")
                if name == "fetch_url":
                    soup = BeautifulSoup(text, "html.parser")
                    for node in soup(["script", "style", "noscript"]):
                        node.decompose()
                    return soup.get_text("\n")[:OUTPUT_LIMIT]
                return f"status: {response.status_code}\nbody (bounded):\n{text}"
    context = RunContext("", "", "", "", payload["id"], cwd, None,
                         tool_output_cap=OUTPUT_LIMIT, shell_output_cap=OUTPUT_LIMIT)
    return await WORKER_TOOLS[name].coroutine(runtime=SimpleNamespace(context=context), **args)


class Worker:
    def __init__(self, resource: str, root: Path, home: Path, controller_uid: int):
        self.resource, self.root, self.home = resource, root, home
        self.controller_uid = controller_uid
        self.busy = False
        # Repeated requests are refused for this worker lifetime. Core never retries,
        # even after worker restart; its durable intent remains authoritative.
        self.seen: set[str] = set()

    async def handle(self, reader, writer):
        proc = None
        tasks = []
        owns_slot = False
        ident = None
        try:
            if peer_uid(writer) != self.controller_uid:
                return
            async with asyncio.timeout(5):
                payload = await receive(reader)
            ident = payload.get("id")
            if (payload.get("version") != VERSION or payload.get("resource") != self.resource
                    or not isinstance(ident, str) or len(ident) != 36 or ident in self.seen
                    or self.busy or len(self.seen) >= 100000):
                raise SandboxError("request refused")
            if payload.get("operation") not in (*WORKER_TOOLS, "probe"):
                raise SandboxError("operation unavailable")
            self.seen.add(ident)
            self.busy = owns_slot = True
            timeout = min(120, max(1, int(payload.get("args", {}).get("timeout", 120))))
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "openbot.sandboxes.worker", "--execute", str(self.root),
                cwd=self.home, env=environment(str(self.home)), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            proc.stdin.write(json.dumps(payload).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            output = asyncio.create_task(bounded(proc.stdout))
            exited = asyncio.create_task(proc.wait())
            disconnected = asyncio.create_task(reader.read(1))
            tasks = [output, exited, disconnected]
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait([exited, disconnected], return_when=asyncio.FIRST_COMPLETED)
                if disconnected in done:
                    raise SandboxError("controller disconnected")
                result = await output
                if exited.result() != 0:
                    raise SandboxError("operation failed; outcome unknown")
                await stop(proc)
                proc = None
                # Finish cleanup before replying so the next request cannot race
                # this operation's finally block and see a spuriously busy worker.
                self.busy = owns_slot = False
                await send(writer, {"version": VERSION, "id": ident, "ok": True, "result": result})
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- never expose request/credential-bearing exceptions
            with suppress(OSError):
                await send(writer, {"version": VERSION, "id": ident, "ok": False})
        finally:
            if proc is not None:
                # Also stop descendants that retained the original group after normal exit.
                with suppress(TimeoutError):
                    await stop(proc)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if owns_slot:
                self.busy = False
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def serve(args):
    if os.getuid() == 0 or args.controller_uid in (0, os.getuid()):
        raise SystemExit("worker and controller require distinct non-root UIDs")
    home = Path.home().resolve()
    if home.stat().st_uid != os.getuid() or home.stat().st_mode & 0o077:
        raise SystemExit("worker home must be owned by its UID with mode 0700")
    root = Path(args.root).resolve(strict=True)
    if not root.is_relative_to(home):
        raise SystemExit("worker root must be inside its private home")
    worker = Worker(args.resource, root, home, args.controller_uid)
    # The worker itself creates the socket, so SO_PEERCRED identifies its UID
    # (a socket activated by root systemd would identify the socket creator).
    # systemd RuntimeDirectory owns the per-slot parent; the controller joins the
    # slot's private IPC group. Other sandbox UIDs get no such membership.
    socket_path = Path(args.socket)
    parent = socket_path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise SystemExit("socket parent must be worker-owned and not group/world writable")
    if socket_path.exists():
        raise SystemExit("socket already exists; stop unit and reconcile before restart")
    os.umask(0o007)
    server = await asyncio.start_unix_server(worker.handle, path=str(socket_path))
    async with server:
        await server.serve_forever()


def main():
    if sys.platform != "linux":
        raise SystemExit("sandbox worker requires Linux")
    if len(sys.argv) == 3 and sys.argv[1] == "--execute":
        result = asyncio.run(operation(Path(sys.argv[2]), json.load(sys.stdin)))
        sys.stdout.write(result[:OUTPUT_LIMIT])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--controller-uid", required=True, type=int)
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
