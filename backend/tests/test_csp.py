import re
from pathlib import Path

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from openbot.api.csp import DEFAULT_POLICY
from openbot.main import create_app


async def get(settings, services, path):
    app = create_app(settings, services=services)
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        return await c.get(path)


async def test_ui_and_api_responses_carry_the_policy(settings, services, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>OpenBot</h1>")
    settings.frontend_dist = dist
    for path in ("/", "/threads/x", "/api/v1/health", "/api/v1/nope"):
        r = await get(settings, services, path)
        assert r.headers["content-security-policy"] == DEFAULT_POLICY, path


def test_default_policy_blocks_remote_loads_and_foreign_scripts():
    directives = dict(d.split(" ", 1) for d in DEFAULT_POLICY.split("; "))
    assert directives["script-src"] == "'self' 'wasm-unsafe-eval'"
    assert directives["img-src"] == "'self' data: blob:"
    assert directives["connect-src"] == "'self' data: blob:"
    assert directives["frame-ancestors"] == "'none'"


async def test_policy_can_be_replaced_or_turned_off(settings, services):
    settings.content_security_policy = "default-src 'self' https://cdn.example"
    assert (await get(settings, services, "/api/v1/health")).headers["content-security-policy"].endswith("cdn.example")
    settings.content_security_policy = ""
    assert "content-security-policy" not in (await get(settings, services, "/api/v1/health")).headers


def test_web_ui_entry_has_no_inline_script():
    """script-src 'self' would block it: the pre-paint theme script lives in public/theme-init.js."""
    html = (Path(__file__).resolve().parents[2] / "frontend" / "index.html").read_text()
    for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.DOTALL):
        assert "src=" in attrs and not body.strip(), attrs
