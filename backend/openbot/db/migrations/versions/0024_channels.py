"""Channel attachments, inbound deduplication and retryable delivery."""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = depends_on = None


def upgrade():
    op.create_table("channel_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("chat_id", sa.String(200), nullable=False),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("channel", "chat_id"))
    op.create_table("channel_receipts",
        sa.Column("channel", sa.String(32), primary_key=True),
        sa.Column("external_id", sa.String(250), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("channel_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36), sa.ForeignKey("channel_bindings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("channel_deliveries")
    op.drop_table("channel_receipts")
    op.drop_table("channel_bindings")
