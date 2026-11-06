"""Host-owned plugin settings documents."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "plugin_settings",
        sa.Column("plugin_id", sa.String(32), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("subject", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("actors.id", ondelete="CASCADE")),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("plugin_settings")
