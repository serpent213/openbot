"""Bounded Linux Unix-socket RPC. Peer credentials, not shared bearer secrets.

One request per connection; never retry an execution after losing its response.
Socket parents must be administrator-owned. The worker is untrusted by core.
"""
from __future__ import annotations

import asyncio
import json
import socket
import struct
from contextlib import suppress

MAX_FRAME = 256 * 1024
VERSION = 1


class SandboxError(RuntimeError):
    pass


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
        raise SandboxError("sandbox frame too large")
    writer.write(struct.pack("!I", len(body)) + body)
    await writer.drain()


async def request(path: str, uid: int, payload: dict, timeout: float = 135) -> str:
    """Authentication failures and lost replies are errors, never local fallbacks."""
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(path)
            if peer_uid(writer) != uid:
                raise SandboxError("unexpected sandbox worker identity")
            await send(writer, {**payload, "version": VERSION})
            reply = await receive(reader)
            if reply.get("id") != payload["id"] or reply.get("version") != VERSION:
                raise SandboxError("invalid sandbox response identity")
            if reply.get("ok") is not True or not isinstance(reply.get("result"), str):
                raise SandboxError("sandbox worker refused the operation")
            return reply["result"]
    except (OSError, ValueError, EOFError, asyncio.IncompleteReadError, TimeoutError) as exc:
        raise SandboxError("sandbox response unavailable; execution outcome may be unknown") from exc
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
