"""Upgrade existing allocations with safe defaults; compile PostgreSQL DDL offline."""
import asyncio
import importlib
import io

from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from openbot.db.session import _alembic_config, make_engine, run_migrations


async def test_existing_sqlite_allocation_defaults_to_private_memory(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/upgrade.db"
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "0025")
    engine = make_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("""INSERT INTO sandboxes
                (id, name, backend, resource, resource_key, resource_spec, generation, state, created_at, updated_at)
                VALUES ('sandbox', 'existing', 'linux', 'slot', 'uid:1234', '{}', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
        await run_migrations(url)
        await run_migrations(url)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT id, share_global_memory FROM sandboxes"))).one()
            assert row.id == "sandbox" and not row.share_global_memory
            columns = (await connection.execute(text("PRAGMA table_info(runs)"))).all()
            assert "memory_namespace" in {column.name for column in columns}
            assert (await connection.execute(text("SELECT count(*) FROM sandbox_scratchpads"))).scalar() == 0
    finally:
        await engine.dispose()


def test_sandbox_memory_migration_compiles_for_postgresql_without_sqlite_specific_sql():
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    migration = importlib.import_module("openbot.db.migrations.versions.0026_sandbox_memory")
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "share_global_memory BOOLEAN DEFAULT false NOT NULL" in sql
    assert "memory_namespace JSON" in sql
    assert "CREATE TABLE sandbox_scratchpads" in sql
    assert "DROP TABLE sandbox_scratchpads" in sql
    assert "PRAGMA" not in sql and "sqlite" not in sql.lower()
