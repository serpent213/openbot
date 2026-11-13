"""Content-Security-Policy for everything this server sends, the web UI above all.

The browser build had none. Message text comes from bots, whose replies can be steered by prompt
injection, so the UI's Markdown sanitizing is the only thing between a reply and the page. The policy
is the backstop: scripts only from this origin (the API key sits in localStorage), and images, media
and fetches only from here, so a rendered reply cannot send data elsewhere by loading a URL.
'unsafe-inline' styles stay allowed: React style props and highlighted code use style attributes.
"""
from __future__ import annotations

DEFAULT_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "  # the 3D lab's decoders are WebAssembly; JS eval stays blocked
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' data: blob:; "  # three.js fetches embedded model data; neither leaves the page
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class ContentSecurityPolicy:
    def __init__(self, app, policy: str):
        self.app, self.header = app, policy.encode("latin-1")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.header:
            return await self.app(scope, receive, send)

        async def with_policy(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"content-security-policy" for k, _ in headers):
                    headers.append((b"content-security-policy", self.header))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_policy)
