"""Version 1 interface for explicitly installed, trusted Python plugins.

Plugins run in-process, with the same OS privileges as Openbot. This interface
defines compatibility and lifecycle, not a sandbox. Events are notifications.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic import command, context
from alembic.config import Config
from fastapi import APIRouter, Request
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from sqlalchemy import Column, String, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from openbot.db.models import Actor, Thread, TimestampMixin, UTCDateTime, new_id, utcnow

if TYPE_CHECKING:
    from openbot.runtime.channels import ChannelHost
    from openbot.runtime.cli_agents.base import CliAdapter
    from openbot.runtime.commands import Command
    from openbot.runtime.plugin_settings import SettingsStore

API_VERSION = (1, 2)

__all__ = [
    "API_VERSION",
    "ActorRef",
    "CliAdapter",
    "CliAgentError",
    "Command",
    "CommandContext",
    "CommandResult",
    "ModelBase",
    "Plugin",
    "PluginHost",
    "PluginSettings",
    "StreamParser",
    "TimestampMixin",
    "ToolContext",
    "Turn",
    "TurnResult",
    "UTCDateTime",
    "UnknownSession",
    "find_actor",
    "host_from_request",
    "host_from_runtime",
    "migration_environment",
    "new_id",
    "thread_exists",
    "tool_context",
    "utcnow",
]


class ModelBase(DeclarativeBase):
    """Plugin ORM metadata, separate from core's migration/create_all metadata."""


# Stable foreign-key targets; plugins query identities through find_actor/thread_exists.
Table("actors", ModelBase.metadata, Column("id", String(36), primary_key=True))
Table("threads", ModelBase.metadata, Column("id", String(36), primary_key=True))


@dataclass(frozen=True, kw_only=True)
class PluginSettings:
    global_: type[BaseModel] | None = None
    agent: type[BaseModel] | None = None
    version: int = 1
    migrate: Callable[[str, int, dict], dict] | None = None


@dataclass(frozen=True)
class ToolContext:
    actor_id: str
    actor_handle: str
    thread_id: str
    run_id: str


def tool_context(runtime) -> ToolContext:
    ctx = runtime.context
    return ToolContext(ctx.actor_id, ctx.actor_handle, ctx.thread_id, ctx.run_id)


@dataclass(frozen=True, kw_only=True)
class Plugin:
    id: str
    api_version: tuple[int, int] = API_VERSION
    version: str = "0.1.0"
    settings: PluginSettings | None = None
    requires: tuple[str, ...] = ()
    tools: tuple[BaseTool, ...] = ()
    commands: tuple[Command, ...] = ()
    backends: tuple[CliAdapter, ...] = ()
    # Public callbacks must authenticate the transport themselves.
    public_router: APIRouter | None = None
    background: Callable[[PluginHost], AbstractAsyncContextManager[None]] | None = None
    router: APIRouter | None = None
    migrations: Path | None = None
    lifespan: Callable[[PluginHost], AbstractAsyncContextManager[None]] | None = None


@dataclass(kw_only=True)
class PluginHost:
    id: str
    engine: AsyncEngine
    session_factory: async_sessionmaker
    state_dir: Path
    config: dict[str, Any]
    _publish: Callable[[str, str | None, dict], Awaitable[None]]
    settings: SettingsStore | None = None
    channels: ChannelHost | None = None
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @property
    def database_name(self) -> str:
        return self.engine.url.database or ""

    async def publish(self, event: str, thread_id: str | None, data: dict) -> None:
        import re
        if not re.fullmatch(re.escape(self.id) + r"\.[a-z0-9_.]+", event):
            raise ValueError("plugin events must use the plugin ID prefix")
        await self._publish(event, thread_id, data)

    async def migrate(self, directory: Path, revision: str = "head") -> None:
        cfg = Config()
        cfg.set_main_option("script_location", str(directory))
        cfg.set_main_option("version_table", f"plugin_{self.id}_version")
        # Share the application engine, including its in-memory SQLite connection.
        async with self.engine.begin() as connection:

            def upgrade(sync_connection):
                cfg.attributes["connection"] = sync_connection
                command.upgrade(cfg, revision)

            await connection.run_sync(upgrade)


def migration_environment() -> None:
    """Called from a plugin's Alembic env.py; host owns the connection/transaction."""
    context.configure(
        connection=context.config.attributes["connection"],
        version_table=context.config.get_main_option("version_table"),
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


@dataclass(frozen=True)
class ActorRef:
    id: str
    handle: str
    kind: str
    name: str


async def find_actor(session, handle: str) -> ActorRef | None:
    actor = await session.scalar(
        select(Actor).where(Actor.handle == handle, Actor.enabled.is_(True))
    )
    return ActorRef(actor.id, actor.handle, actor.kind, actor.name) if actor else None


async def thread_exists(session, thread_id: str) -> bool:
    thread = await session.get(Thread, thread_id)
    return thread is not None and thread.deleted_at is None


def host_from_request(request: Request, plugin_id: str) -> PluginHost:
    return request.app.state.services.plugin_hosts[plugin_id]


def host_from_runtime(runtime, plugin_id: str) -> PluginHost:
    return runtime.context.services.plugin_hosts[plugin_id]

# Stable plugin extension contracts.
from openbot.runtime.cli_agents.base import (
    CliAdapter,
    CliAgentError,
    StreamParser,
    Turn,
    TurnResult,
    UnknownSession,
)
from openbot.runtime.commands import Command, CommandContext, CommandResult
