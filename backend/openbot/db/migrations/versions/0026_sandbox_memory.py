"""Opt-in global memory, per-run scope snapshots and sandbox-scoped scratchpads."""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = depends_on = None


def upgrade():
    op.add_column("sandboxes", sa.Column("share_global_memory", sa.Boolean(), nullable=False,
                                         server_default=sa.false()))
    op.add_column("runs", sa.Column("memory_namespace", sa.JSON(), nullable=True))
    op.create_table("sandbox_scratchpads",
        sa.Column("sandbox_id", sa.String(36), sa.ForeignKey("sandboxes.id"), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("sandbox_scratchpads")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("memory_namespace")
    with op.batch_alter_table("sandboxes") as batch:
        batch.drop_column("share_global_memory")
