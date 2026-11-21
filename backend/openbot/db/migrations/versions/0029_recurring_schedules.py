"""recurring schedules

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "recurring_schedules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("thread_id", sa.String(36), nullable=False),
        sa.Column("sender_actor_id", sa.String(36), nullable=False),
        sa.Column("to_handles", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("cron", sa.String(120), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_id", sa.String(36), nullable=True),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recurring_status_due", "recurring_schedules", ["status", "next_due_at"])
    op.add_column("scheduled_messages", sa.Column("recurring_id", sa.String(36), nullable=True))


def downgrade():
    op.drop_column("scheduled_messages", "recurring_id")
    op.drop_index("ix_recurring_status_due", table_name="recurring_schedules")
    op.drop_table("recurring_schedules")
