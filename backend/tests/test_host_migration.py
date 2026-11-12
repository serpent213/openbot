"""No existing NULL binding becomes a privileged opt-in on upgrade."""
import asyncio
import importlib
import io

from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from openbot.db.session import _alembic_config, make_engine, run_migrations


async def test_upgrade_preserves_unbound_threads_without_enabling_host(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/upgrade.db"
    await asyncio.to_thread(command.upgrade, _alembic_config(url), "0026")
    engine = make_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("""INSERT INTO threads
                (id, title, kind, hop_limit_notified, auto_renamed, created_at, updated_at)
                VALUES ('old', 'Existing', 'chat', false, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """))
        await run_migrations(url)
        await run_migrations(url)
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT sandbox_id, host_execution FROM threads WHERE id='old'"))).one()
            assert row.sandbox_id is None and row.host_execution == 0
            columns = (await connection.execute(text("PRAGMA table_info(runs)"))).all()
            assert next(c for c in columns if c.name == "host_execution").dflt_value == "0"
    finally:
        await engine.dispose()


def test_host_migration_postgresql_ddl():
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    migration = importlib.import_module("openbot.db.migrations.versions.0027_host_execution")
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ALTER TABLE threads ADD COLUMN host_execution BOOLEAN DEFAULT false NOT NULL" in sql
    assert "ALTER TABLE runs ADD COLUMN host_execution BOOLEAN DEFAULT false NOT NULL" in sql
    assert "DROP COLUMN host_execution" in sql
    assert "PRAGMA" not in sql
