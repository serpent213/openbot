"""Version 1 interface for explicitly installed, trusted Python plugins.

Plugins run in-process, with the same OS privileges as Openbot. This interface
defines compatibility and lifecycle, not a sandbox. Events are notifications.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alembic import command, context
from alembic.config import Config
from fastapi import APIRouter, Request
from langchain_core.tools import BaseTool
from sqlalchemy import Column, String, Table, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from openbot.db.models import Actor, Thread, TimestampMixin, UTCDateTime, new_id, utcnow
from openbot.tools.context import RunContext

__all__ = [
    "ActorRef",
    "ModelBase",
    "Plugin",
    "PluginHost",
    "RunContext",
    "TimestampMixin",
    "UTCDateTime",
    "find_actor",
    "host_from_request",
    "host_from_runtime",
    "migration_environment",
    "new_id",
    "thread_exists",
    "utcnow",
]


class ModelBase(DeclarativeBase):
    """Plugin ORM metadata, separate from core's migration/create_all metadata."""


# Stable foreign-key targets; plugins query identities through find_actor/thread_exists.
Table("actors", ModelBase.metadata, Column("id", String(36), primary_key=True))
Table("threads", ModelBase.metadata, Column("id", String(36), primary_key=True))


@dataclass(frozen=True)
class Plugin:
    id: str
    api_version: int = 1
    requires: tuple[str, ...] = ()
    tools: tuple[BaseTool, ...] = ()
    router: APIRouter | None = None
    migrations: Path | None = None
    lifespan: Callable | None = None  # async context manager receiving PluginHost


@dataclass
class PluginHost:
    id: str
    database_url: str
    session_factory: async_sessionmaker
    state_dir: Path
    config: dict
    publish: Callable  # (event, thread_id, data); best-effort UI notification only
    state: dict[str, Any] = field(default_factory=dict)

    async def migrate(self, directory: Path, revision: str = "head") -> None:
        cfg = Config()
        cfg.set_main_option("script_location", str(directory))
        cfg.set_main_option("sqlalchemy.url", self.database_url.replace("%", "%%"))
        cfg.set_main_option("version_table", f"plugin_{self.id}_version")
        # Share the application engine, including its in-memory SQLite connection.
        async with self.session_factory.kw["bind"].begin() as connection:

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
