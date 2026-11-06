"""Authenticated operator settings; deliberately absent from agent configuration tools."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openbot.api.deps import get_services
from openbot.db.models import Actor
from openbot.plugins import configured_plugins
from openbot.runtime.plugin_settings import SettingsConflict, SettingsStore

router = APIRouter()


class SettingsUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    value: dict


def store(services, plugin_id, scope):
    plugin = next((p for p in configured_plugins(services) if p.id == plugin_id), None)
    if plugin is None:
        raise HTTPException(404, "plugin is not enabled")
    result = SettingsStore(services, plugin)
    try:
        result.model(scope)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return result


async def agent(services, actor_id):
    async with services.session_factory() as session:
        who = await session.get(Actor, actor_id)
        if who is None or who.kind != "bot":
            raise HTTPException(404, "unknown bot")


@router.get("/plugins")
async def plugins(services=Depends(get_services)):
    return [
        {
            "id": p.id,
            "version": p.version,
            "api_version": p.api_version,
            "tools": [t.name for t in p.tools],
            "settings_scopes": [
                s
                for s in ("global", "agent")
                if p.settings and getattr(p.settings, "global_" if s == "global" else s)
            ],
        }
        for p in configured_plugins(services)
    ]


@router.get("/plugins/{plugin_id}/settings")
async def read_global(plugin_id: str, services=Depends(get_services)):
    return await store(services, plugin_id, "global").describe("global")


@router.get("/bots/{actor_id}/plugin-settings/{plugin_id}")
async def read_agent(actor_id: str, plugin_id: str, services=Depends(get_services)):
    await agent(services, actor_id)
    return await store(services, plugin_id, "agent").describe("agent", actor_id)


async def write(services, plugin_id, scope, subject, body, replace):
    try:
        return await store(services, plugin_id, scope).write(
            scope, subject, body.value, body.expected_revision, replace=replace
        )
    except SettingsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.patch("/plugins/{plugin_id}/settings")
async def patch_global(plugin_id: str, body: SettingsUpdate, services=Depends(get_services)):
    return await write(services, plugin_id, "global", "", body, False)


@router.put("/plugins/{plugin_id}/settings")
async def put_global(plugin_id: str, body: SettingsUpdate, services=Depends(get_services)):
    return await write(services, plugin_id, "global", "", body, True)


@router.patch("/bots/{actor_id}/plugin-settings/{plugin_id}")
async def patch_agent(
    actor_id: str, plugin_id: str, body: SettingsUpdate, services=Depends(get_services)
):
    await agent(services, actor_id)
    return await write(services, plugin_id, "agent", actor_id, body, False)


@router.put("/bots/{actor_id}/plugin-settings/{plugin_id}")
async def put_agent(
    actor_id: str, plugin_id: str, body: SettingsUpdate, services=Depends(get_services)
):
    await agent(services, actor_id)
    return await write(services, plugin_id, "agent", actor_id, body, True)
