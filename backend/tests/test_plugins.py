import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from langchain.tools import tool

from openbot.config import Settings
from openbot.main import create_app
from openbot.plugin_api import Plugin
from openbot.plugins import load_plugins, start_plugins, stop_plugins
from openbot.tools.registry import build_registry


def install(monkeypatch, name, **kwargs):
    plugin = Plugin(id=name, **kwargs)
    monkeypatch.setitem(sys.modules, "example_" + name, SimpleNamespace(plugin=plugin))
    return "example_" + name


def test_explicit_loading_versions_and_dependencies(monkeypatch):
    a = install(monkeypatch, "first")
    b = install(monkeypatch, "second", requires=("first",))
    assert [p.id for p in load_plugins([b, a])] == ["first", "second"]
    with pytest.raises(ValueError, match="dependencies"):
        load_plugins([b])
    with pytest.raises(ValueError, match="duplicate"):
        load_plugins([a, a])
    bad = install(monkeypatch, "bad", api_version=99)
    with pytest.raises(ValueError, match="incompatible"):
        load_plugins([bad])


def test_tools_cannot_replace_core_tools(monkeypatch, tmp_path):
    @tool
    def run_shell() -> str:
        """A conflicting tool."""
        return "no"

    module = install(monkeypatch, "conflict", tools=(run_shell,))
    with pytest.raises(ValueError, match="duplicate tool"):
        build_registry(Settings(_env_file=None, plugin_modules=[module], tools_dir=tmp_path))


async def test_lifecycle_closes_in_reverse_order_and_on_failure(monkeypatch, services, tmp_path):
    events = []

    @asynccontextmanager
    async def life(host):
        events.append("start " + host.id)
        try:
            yield
        finally:
            events.append("stop " + host.id)

    a = install(monkeypatch, "first", lifespan=life)
    b = install(monkeypatch, "second", lifespan=life, requires=("first",))
    services.settings.plugin_modules = [b, a]
    services.settings.plugin_state_dir = tmp_path
    await start_plugins(services)
    await start_plugins(services)
    await stop_plugins(services)
    assert events == ["start first", "start second", "stop second", "stop first"]

    @asynccontextmanager
    async def broken(_host):
        raise RuntimeError("broken")
        yield

    bad = install(monkeypatch, "bad", lifespan=broken, requires=("first",))
    services.settings.plugin_modules = [a, bad]
    services.plugins = None
    with pytest.raises(RuntimeError, match="broken"):
        await start_plugins(services)
    assert events[-2:] == ["start first", "stop first"]
    assert services.plugin_hosts == {}


async def test_plugin_routes_inherit_auth_and_reject_collisions(monkeypatch, services):
    router = APIRouter(prefix="/example")

    @router.get("")
    def read():
        return {"ok": True}

    module = install(monkeypatch, "example", router=router)
    services.settings.plugin_modules = [module]
    services.settings.openbot_api_key = "secret"
    app = create_app(services.settings, services)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/example")).status_code == 401
        assert (await client.get("/api/v1/example", headers={"X-API-Key": "secret"})).json() == {
            "ok": True
        }
    other = install(monkeypatch, "other", router=router)
    services.settings.plugin_modules.append(other)
    services.plugins = None
    with pytest.raises(ValueError, match="prefix"):
        create_app(services.settings, services)


async def test_plugins_close_if_later_startup_fails(monkeypatch, services, tmp_path):
    from asgi_lifespan import LifespanManager

    from openbot import main

    closed = []

    @asynccontextmanager
    async def life(host):
        try:
            yield
        finally:
            closed.append(host.id)

    async def fail(_services):
        raise RuntimeError("later startup failed")

    services.settings.plugin_modules = [install(monkeypatch, "example", lifespan=life)]
    services.settings.plugin_state_dir = tmp_path
    monkeypatch.setattr(main, "ensure_human_actor", fail)
    with pytest.raises(RuntimeError, match="later startup failed"):
        async with LifespanManager(create_app(services.settings, services)):
            pass
    assert closed == ["example"]
    assert services.plugin_hosts == {}


async def test_plugin_context_events_and_single_manifest(monkeypatch, services, tmp_path):
    from openbot.plugin_api import ToolContext, tool_context

    module = install(monkeypatch, "example", api_version=(1, 0))
    services.settings.plugin_modules = [module]
    services.settings.plugin_state_dir = tmp_path
    # Services are authoritative even if create_app is passed different settings.
    app = create_app(Settings(_env_file=None), services)
    assert app.state.settings is services.settings
    monkeypatch.delitem(sys.modules, module)
    await start_plugins(services)  # Uses the manifest already loaded by create_app.
    host = services.plugin_hosts["example"]
    await host.publish("example.updated", None, {})
    with pytest.raises(ValueError, match="prefix"):
        await host.publish("other.updated\nevent: forged", None, {})
    ctx = tool_context(SimpleNamespace(context=SimpleNamespace(
        actor_id="a", actor_handle="agent", thread_id="t", run_id="r", services=services)))
    assert isinstance(ctx, ToolContext) and not hasattr(ctx, "services")
    await stop_plugins(services)
    future = install(monkeypatch, "future", api_version=(1, 99))
    with pytest.raises(ValueError, match="incompatible"):
        load_plugins([future])
