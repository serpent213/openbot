"""Optional persistent sandbox bindings and execution intents."""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = depends_on = None


def upgrade():
    op.create_table("sandboxes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("backend", sa.String(32), nullable=False),
        sa.Column("resource", sa.String(120), nullable=False),
        sa.Column("resource_key", sa.String(200), unique=True, nullable=False),
        sa.Column("resource_spec", sa.JSON(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("active_run_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("backend", "resource", name="uq_sandbox_resource"))
    with op.batch_alter_table("threads") as batch:
        batch.add_column(sa.Column("sandbox_id", sa.String(36)))
        batch.create_foreign_key("fk_thread_sandbox", "sandboxes", ["sandbox_id"], ["id"])
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("sandbox_id", sa.String(36)))
        batch.add_column(sa.Column("sandbox_generation", sa.Integer()))
        batch.create_foreign_key("fk_run_sandbox", "sandboxes", ["sandbox_id"], ["id"])
    op.create_table("sandbox_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sandbox_id", sa.String(36), sa.ForeignKey("sandboxes.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("sandbox_executions")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_run_sandbox", type_="foreignkey")
        batch.drop_column("sandbox_generation")
        batch.drop_column("sandbox_id")
    with op.batch_alter_table("threads") as batch:
        batch.drop_constraint("fk_thread_sandbox", type_="foreignkey")
        batch.drop_column("sandbox_id")
    op.drop_table("sandboxes")
