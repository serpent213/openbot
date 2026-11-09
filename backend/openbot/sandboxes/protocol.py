"""Authenticated, framed local RPC with streamed events and explicit outcomes.

One execution per connection; never replay after losing a terminal response.
A known rejection is different from transport loss or uncertain execution.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from contextlib import asynccontextmanager, suppress

MAX_FRAME = 32 * 1024 * 1024  # matches the existing coding-CLI event limit
VERSION = 2


class SandboxError(RuntimeError):
    """Unknown outcome or unavailable execution boundary."""


class SandboxRejected(SandboxError):
    """Known preflight rejection: no agent-directed operation was executed."""


def peer_uid(writer) -> int:
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        raise SandboxError("sandbox transport requires Linux peer credentials")
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


async def receive(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise SandboxError("invalid sandbox frame size")
    value = json.loads(await reader.readexactly(size))
    if not isinstance(value, dict):
        raise SandboxError("invalid sandbox frame")
    return value


async def send(writer, value):
    body = json.dumps(value, ensure_ascii=True).encode()
    if len(body) > MAX_FRAME:
        raise SandboxError("sandbox response exceeds CLI event limit; outcome may be unknown")
    writer.write(struct.pack("!I", len(body)) + body)
    await writer.drain()


@asynccontextmanager
async def run_lease(path: str, uid: int, payload: dict):
    """A control connection lives for the whole run. EOF also triggers run cleanup."""
    writer = None
    started = False
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        if peer_uid(writer) != uid:
            raise SandboxError("unexpected sandbox worker identity")
        await send(writer, {**payload, "version": VERSION, "operation": "run"})
        async with asyncio.timeout(30):
            ready = await receive(reader)
        if ready.get("id") != payload["id"] or ready.get("version") != VERSION:
            raise SandboxError("invalid worker lease response")
        if ready.get("type") == "rejected":
            raise SandboxRejected(str(ready.get("error")))
        if ready.get("type") != "ready":
            raise SandboxError("worker run admission failed")
        started = True
        yield ready.get("result")
    finally:
        try:
            if writer is not None and started:
                async with asyncio.timeout(30):
                    await send(writer, {"finish": True})
                    final = await receive(reader)
                if (final.get("type") != "result" or final.get("id") != payload["id"]
                        or final.get("version") != VERSION or final.get("result") != "clean"):
                    raise SandboxError("whole-run cleanup unconfirmed; sandbox must be quarantined")
        except (OSError, EOFError, ValueError, TimeoutError) as exc:
            raise SandboxError("whole-run cleanup unavailable; sandbox must be quarantined") from exc
        finally:
            if writer is not None:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()


async def request(path: str, uid: int, payload: dict, timeout: float | None = None, on_event=None):
    """Return a terminal result, or raise a known rejection/unknown-outcome error.

    Tool timeouts belong to their implementations; CLI timeout belongs to run_turn.
    There is deliberately no general 120-second execution deadline.
    """
    frame = {**payload, "version": VERSION}
    try:
        if len(json.dumps(frame, ensure_ascii=True).encode()) > MAX_FRAME:
            raise SandboxRejected("sandbox request exceeds CLI event limit; nothing was dispatched")
    except (ValueError, TypeError) as exc:
        raise SandboxRejected("sandbox request is not JSON serializable; nothing was dispatched") from exc
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(path)
            if peer_uid(writer) != uid:
                raise SandboxError("unexpected sandbox worker identity")
            await send(writer, frame)
            while True:
                reply = await receive(reader)
                if reply.get("id") != payload["id"] or reply.get("version") != VERSION:
                    raise SandboxError("invalid sandbox response identity")
                kind = reply.get("type")
                if kind == "event":
                    if on_event is not None:
                        await on_event(reply["kind"], reply.get("payload"))
                elif kind == "result":
                    return reply.get("result")
                elif kind == "rejected":
                    raise SandboxRejected(str(reply.get("error", "worker rejected operation")))
                else:
                    raise SandboxError("sandbox execution outcome unknown; inspect before retrying")
    except (OSError, ValueError, EOFError, asyncio.IncompleteReadError, TimeoutError) as exc:
        raise SandboxError("sandbox response unavailable; execution outcome may be unknown") from exc
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
