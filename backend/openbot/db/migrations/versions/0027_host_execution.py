"""Explicit immutable host-thread selection and per-run snapshot; never infer from NULL."""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = depends_on = None


def upgrade():
    for table in ("threads", "runs"):
        op.add_column(table, sa.Column("host_execution", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade():
    for table in ("runs", "threads"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("host_execution")
