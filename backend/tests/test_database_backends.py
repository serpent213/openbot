"""Real database checks. PostgreSQL uses a new disposable database per test.

Set OPENBOT_TEST_POSTGRES_URL to a local PostgreSQL URL with CREATEDB privilege.
No existing database is migrated or dropped by these tests.
"""
import os
from uuid import uuid4

import pytest
from langchain_core.embeddings import Embeddings
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url

from openbot.api.settings import database_location
from openbot.config import Settings
from openbot.db.models import Actor, Thread
from openbot.db.session import (
    _alembic_config,
    downgrade_migrations,
    make_engine,
    make_session_factory,
    run_migrations,
)
from openbot.runtime.persistence import open_langgraph_backends


@pytest.fixture(params=["sqlite", "postgresql"])
async def database_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path}/app.db"
        return
    configured = os.environ.get("OPENBOT_TEST_POSTGRES_URL")
    if not configured:
        pytest.skip("set OPENBOT_TEST_POSTGRES_URL to test PostgreSQL")
    import psycopg
    from psycopg import sql

    base = make_url(configured).set(drivername="postgresql")
    name = "openbot_test_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(
        base.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield base.set(drivername="postgresql+psycopg", database=name).render_as_string(
                hide_password=False
            )
        finally:
            await admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_migration_url_preserves_percent_encoding():
    url = "postgresql+psycopg://user:encoded%25password@localhost/db?options=-csearch_path%3Dpublic"
    assert _alembic_config(url).get_main_option("sqlalchemy.url") == url


async def test_migrations_crud_and_downgrade(database_url):
    await run_migrations(database_url)
    await run_migrations(database_url)  # Restart is idempotent.
    engine = make_engine(database_url)
    try:
        sessions = make_session_factory(engine)
        async with sessions() as session:
            actor = Actor(kind="human", handle="you", name="You")
            thread = Thread(title="Database test")
            session.add_all([actor, thread])
            await session.commit()
            actor_id = actor.id
        async with sessions() as session:
            actor = await session.get(Actor, actor_id)
            assert actor.enabled is True
            actor.name = "Rolled back"
            await session.rollback()
        async with sessions() as session:
            assert (await session.get(Actor, actor_id)).name == "You"
            assert (await session.scalar(select(Thread))).auto_renamed is False
        await downgrade_migrations(database_url, "base")
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
            assert "actors" not in tables and "threads" not in tables
        await run_migrations(database_url)
    finally:
        await engine.dispose()


class LocalEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] if "apple" in t else [0.0, 1.0, 0.0] for t in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]


@pytest.mark.parametrize("semantic", [False, True])
async def test_persistence_survives_reopen(database_url, semantic):
    settings = Settings(_env_file=None, database_url=database_url, embedding_dims=3)
    embedding = LocalEmbeddings() if semantic else None
    namespace = ("bots", "example", "memories")
    config = {"configurable": {"thread_id": "example", "checkpoint_ns": ""}}
    async with open_langgraph_backends(settings, embedding) as (saver, store):
        await store.aput(namespace, "a", {"content": "apple preference"})
        await store.aput(namespace, "b", {"content": "orange preference"})
        saved = await saver.aput(config, empty_checkpoint(), {"source": "input", "step": -1}, {})
    async with open_langgraph_backends(settings, embedding) as (saver, store):
        assert (await store.aget(namespace, "a")).value == {"content": "apple preference"}
        assert await saver.aget_tuple(saved) is not None
        if semantic:
            result = await store.asearch(namespace, query="apple", limit=1)
            assert result[0].key == "a" and result[0].score > 0.99


def test_database_location_hides_postgres_password():
    assert database_location("postgresql+psycopg://user:secret@localhost/db") == (
        "postgresql+psycopg://user:***@localhost/db"
    )
