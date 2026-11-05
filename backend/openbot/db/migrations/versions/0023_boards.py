"""Shared project boards and versioned task history.

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = depends_on = None


def upgrade():
    op.create_table("boards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("board_cards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("board_id", sa.String(36), sa.ForeignKey("boards.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("assignee_id", sa.String(36), sa.ForeignKey("actors.id", ondelete="SET NULL")),
        sa.Column("thread_id", sa.String(36), sa.ForeignKey("threads.id", ondelete="SET NULL")),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_board_cards_board_status", "board_cards", ["board_id", "status"])
    op.create_table("board_card_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("card_id", sa.String(36), sa.ForeignKey("board_cards.id"), nullable=False),
        sa.Column("actor_handle", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("card_id", "version", name="uq_card_event_version"))


def downgrade():
    op.drop_table("board_card_events")
    op.drop_index("ix_board_cards_board_status", table_name="board_cards")
    op.drop_table("board_cards")
    op.drop_table("boards")
