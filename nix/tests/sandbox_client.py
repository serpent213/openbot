"""Controller-side probe for the sandbox VM test and post-deploy checks.

Speaks the worker RPC directly, as the controller UID, so no model provider,
database or server is involved. Every command prints one JSON value.

  sandbox_client.py shell SLOT COMMAND [TIMEOUT]   run_shell in a fresh run lease
  sandbox_client.py parallel SLOT                  cancel one of two parallel operations
  sandbox_client.py hold SLOT                      start a long operation and never finish
  sandbox_client.py connect SOCKET                 can this identity reach a worker socket?
"""
from __future__ import annotations

import asyncio
import json
import pwd
import socket
import struct
import sys
import uuid
from pathlib import Path

from openbot.sandboxes import protocol


def endpoint(slot):
    return f"/run/{slot}/worker.sock", pwd.getpwnam(slot).pw_uid


def shell_request(slot, run_id, command, timeout):
    return {"id": str(uuid.uuid4()), "resource": slot, "run_id": run_id, "operation": "run_shell",
            "args": {"command": command, "timeout": timeout}}


async def lease(slot):
    path, uid = endpoint(slot)
    run_id = uuid.uuid4().hex
    return run_id, protocol.run_lease(path, uid, {"id": str(uuid.uuid4()), "resource": slot, "run_id": run_id})


def running(marker):
    """Visible to the controller: NixOS does not mount /proc with hidepid."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if marker in (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"):
                    return True
            except OSError:
                pass
    return False


async def shell(slot, command, timeout="30"):
    path, uid = endpoint(slot)
    run_id, context = await lease(slot)
    async with context:
        return await protocol.request(path, uid, shell_request(slot, run_id, command, int(timeout)))


async def until(condition, seconds):
    for _ in range(int(seconds * 10)):
        if condition():
            return True
        await asyncio.sleep(0.1)
    return False


async def parallel(slot):
    path, uid = endpoint(slot)
    run_id, context = await lease(slot)
    async with context:
        sibling = asyncio.create_task(protocol.request(
            path, uid, shell_request(slot, run_id, "sleep 40; echo sibling-done", 60)))
        doomed = asyncio.create_task(protocol.request(
            path, uid, shell_request(slot, run_id, "sleep 1003; echo never", 60)))
        # Operation startup (interpreter, namespaces) takes seconds; wait for the real commands.
        started = await until(lambda: running("sleep 1003") and running("sleep 40"), 60)
        doomed.cancel()  # drops the connection; the worker kills that operation's cgroup only
        try:
            await doomed
            cancelled = "completed"
        except (asyncio.CancelledError, protocol.SandboxError) as exc:
            cancelled = type(exc).__name__
        state = {"started": started, "cancelled": cancelled,
                 "cancelled_gone": await until(lambda: not running("sleep 1003"), 15),
                 "sibling_alive_meanwhile": not sibling.done() and running("sleep 40")}
        state["sibling"] = await sibling
        return state


async def hold(slot):
    path, uid = endpoint(slot)
    run_id, context = await lease(slot)
    async with context:
        await protocol.request(path, uid, shell_request(slot, run_id, "sleep 1004", 600))


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(path)
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "missing"
    body = json.dumps({"version": protocol.VERSION, "id": str(uuid.uuid4()), "resource": Path(path).parent.name,
                       "operation": "run", "run_id": uuid.uuid4().hex}).encode()
    try:
        sock.sendall(struct.pack("!I", len(body)) + body)
        return "reply" if sock.recv(4) else "no-reply"
    except (ConnectionError, TimeoutError):
        return "no-reply"
    finally:
        sock.close()


def main():
    command, *args = sys.argv[1:]
    if command == "connect":
        result = connect(*args)
    else:
        result = asyncio.run({"shell": shell, "parallel": parallel, "hold": hold}[command](*args))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
