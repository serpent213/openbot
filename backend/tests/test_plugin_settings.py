"""Document validation, concurrency and lifecycle against SQLite and PostgreSQL."""

from typing import Literal

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, update

from openbot.config import Settings
from openbot.db.models import Actor, PluginSetting
from openbot.db.session import make_engine, make_session_factory, run_migrations
from openbot.main import create_app
from openbot.plugin_api import Plugin, PluginSettings
from openbot.runtime.plugin_settings import MASK, SettingsConflict, SettingsStore
from openbot.services import Services
from tests.test_database_backends import database_url

__all__ = ["database_url"]


class Global(BaseModel):
    enabled: bool = Field(False, json_schema_extra={"env": "TEST_PLUGIN_ENABLED"})
    count: int = Field(1, ge=1, le=10)
    token: SecretStr | None = None


class Agent(BaseModel):
    access: Literal["off", "ro", "rw"] = "off"


@pytest.fixture
async def config_store(database_url, tmp_path):
    await run_migrations(database_url)
    engine = make_engine(database_url)
    settings = Settings(_env_file=None, database_url=database_url, secret_key_file=tmp_path / "key")
    plugin = Plugin(id="example", settings=PluginSettings(global_=Global, agent=Agent))
    services = Services(settings, make_session_factory(engine), plugins=[plugin])
    store = SettingsStore(services, plugin)
    async with services.session_factory() as session:
        bot = Actor(kind="bot", handle="test", name="Test")
        session.add(bot)
        await session.commit()
        actor_id = bot.id
    try:
        yield store, actor_id
    finally:
        await engine.dispose()


async def test_secrets_pins_revision_and_validation(config_store, monkeypatch):
    store, _ = config_store
    assert (await store.describe("global"))["revision"] == 0
    row = await store.write("global", "", {"token": "private-value", "count": 2}, 0)
    assert row["value"]["token"] == MASK
    async with store.services.session_factory() as session:
        stored = await session.get(PluginSetting, ("example", "global", ""))
        assert "private-value" not in str(stored.value)
    with pytest.raises(SettingsConflict):
        await store.write("global", "", {"count": 3}, 0)
    with pytest.raises(ValueError, match="invalid"):
        await store.write("global", "", {"count": 0, "token": "never-in-error"}, 1)
    with pytest.raises(ValueError, match="invalid"):
        await store.write("global", "", {"unknown": True}, 1)
    await store.write("global", "", {"token": MASK, "count": 3}, 1, replace=True)
    assert (await store.global_settings()).token.get_secret_value() == "private-value"
    monkeypatch.setenv("TEST_PLUGIN_ENABLED", "true")
    assert (await store.describe("global"))["locked"] == ["enabled"]
    assert (await store.global_settings()).enabled
    with pytest.raises(ValueError, match="locked"):
        await store.write("global", "", {"enabled": False}, 2)
    store.services.settings.plugin_config = {"example": {"count": 8}}
    assert (await store.global_settings()).count == 8
    await store.write("global", "", {"token": None}, 2)
    assert (await store.global_settings()).token is None
    store.services.settings.plugin_config = {}
    assert (await store.global_settings()).count == 3


async def test_invalid_document_fails_closed_repair_and_delete(config_store):
    store, actor = config_store
    await store.write("agent", actor, {"access": "rw"}, 0)
    async with store.services.session_factory() as session:
        await session.execute(update(PluginSetting).values(value={"access": "unknown"}))
        await session.commit()
    with pytest.raises(ValueError):
        await store.for_agent(actor)
    description = await store.describe("agent", actor)
    assert description["error"] and description["value"] is None and description["revision"] == 1
    await store.write("agent", actor, {}, 1, replace=True)
    assert (await store.for_agent(actor)).access == "off"
    async with store.services.session_factory() as session:
        await session.delete(await session.get(Actor, actor))
        await session.commit()
        assert (await session.scalars(select(PluginSetting))).all() == []


async def test_migration_and_one_time_import(config_store):
    store, actor = config_store
    await store.initialize_agent(actor, {"access": "ro"})
    await store.initialize_agent(actor, {"access": "rw"})
    assert (await store.for_agent(actor)).access == "ro"
    store.plugin = Plugin(id="example", settings=PluginSettings(agent=Agent, version=2))
    with pytest.raises(ValueError, match="migration"):
        await store.for_agent(actor)
    store.plugin = Plugin(
        id="example",
        settings=PluginSettings(
            agent=Agent, version=2, migrate=lambda scope, version, value: {"access": "off"}
        ),
    )
    assert (await store.for_agent(actor)).access == "off"
    await store.write("agent", actor, {"access": "rw"}, 1)
    async with store.services.session_factory() as session:
        assert (await session.get(PluginSetting, ("example", "agent", actor))).schema_version == 2


async def test_settings_api_auth_scope_and_conflict(config_store):
    store, actor = config_store
    services = store.services
    services.settings.openbot_api_key = "operator"
    async with AsyncClient(
        transport=ASGITransport(app=create_app(services=services)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/plugins")).status_code == 401
        client.headers["X-API-Key"] = "operator"
        assert (await client.get("/api/v1/plugins")).json()[0]["settings_scopes"] == [
            "global",
            "agent",
        ]
        path = f"/api/v1/bots/{actor}/plugin-settings/example"
        body = {"expected_revision": 0, "value": {"access": "ro"}}
        assert (await client.patch(path, json=body)).status_code == 200
        assert (await client.patch(path, json=body)).status_code == 409
        assert (await client.get("/api/v1/plugins/missing/settings")).status_code == 404
        assert (await client.get("/api/v1/bots/missing/plugin-settings/example")).status_code == 404
