import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from openbot.api.hosts import host_name
from openbot.config import Settings
from openbot.main import create_app


@pytest.mark.parametrize("header, expected", [
    ("localhost:8000", "localhost"),
    ("127.0.0.1", "127.0.0.1"),
    ("[::1]:8000", "::1"),
    ("[::1]", "::1"),
    ("OpenBot.LAN.example.:443", "openbot.lan.example"),
    ("", ""),
    ("[::1", ""),
])
def test_host_name(header, expected):
    assert host_name(header) == expected


def test_trusted_hosts_defaults_and_overrides():
    plain = Settings(_env_file=None, public_url="https://openbot.lan.example")
    assert plain.trusted_hosts() == {"localhost", "127.0.0.1", "::1", "openbot.lan.example"}
    csv = Settings(_env_file=None, allowed_hosts="a.example, B.example.")
    assert {"a.example", "b.example"} <= csv.trusted_hosts()
    # Deployment tooling writes lists as JSON.
    assert "c.example" in Settings(_env_file=None, allowed_hosts='["c.example"]').trusted_hosts()
    assert Settings(_env_file=None, allowed_hosts="*").trusted_hosts() is None


async def request(settings, services, host, path="/api/v1/health"):
    app = create_app(settings, services=services)
    async with LifespanManager(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as c:
        return await c.get(path, headers={"Host": host})


async def test_foreign_host_is_refused_before_any_route(settings, services):
    """DNS rebinding: the browser sends the attacker's name. Public routes are refused too."""
    for path in ("/api/v1/health", "/api/v1/bots"):
        r = await request(settings, services, "attacker.example:8000", path)
        assert r.status_code == 400 and "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
async def test_loopback_hosts_are_served(settings, services, host):
    assert (await request(settings, services, host)).status_code == 200


async def test_public_url_and_configured_aliases_are_served(settings, services):
    settings.public_url = "https://openbot.lan.example"
    settings.allowed_hosts = ["openbot.vpn.example"]
    assert (await request(settings, services, "openbot.lan.example")).status_code == 200
    assert (await request(settings, services, "openbot.vpn.example:443")).status_code == 200
    assert (await request(settings, services, "other.example")).status_code == 400


async def test_wildcard_disables_the_check(settings, services):
    settings.allowed_hosts = ["*"]
    assert (await request(settings, services, "anything.example")).status_code == 200
