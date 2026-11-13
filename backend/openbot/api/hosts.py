"""Refuse requests whose Host header names a host this server does not answer to.

DNS rebinding: a web page on attacker.example re-resolves its own name to 127.0.0.1 and is then
same-origin with this server as far as the browser is concerned. The Host header still says
attacker.example, so checking it closes that path for every route, the public ones included.
Starlette's TrustedHostMiddleware splits on the first colon and so cannot match "[::1]:8000".
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse


def host_name(header: str) -> str:
    value = header.strip().lower()
    if value.startswith("["):  # [v6]:port
        value = value[1:value.find("]")] if "]" in value else ""
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


class HostAllowlist:
    def __init__(self, app, hosts: frozenset[str] | None):
        self.app, self.hosts = app, hosts

    async def __call__(self, scope, receive, send):
        if self.hosts is not None and scope["type"] in ("http", "websocket"):
            header = next((v for k, v in scope["headers"] if k == b"host"), b"").decode("latin-1")
            if host_name(header) not in self.hosts:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await PlainTextResponse("Invalid Host header", status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)
