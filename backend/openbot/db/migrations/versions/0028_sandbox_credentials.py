"""Named credentials with explicit sandbox-only grants."""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = depends_on = None


def upgrade():
    op.create_table("sandbox_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), unique=True, nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("target", sa.String(1000), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("sandbox_credential_grants",
        sa.Column("sandbox_id", sa.String(36), sa.ForeignKey("sandboxes.id"), primary_key=True),
        sa.Column("credential_id", sa.String(36), sa.ForeignKey("sandbox_credentials.id"), primary_key=True))


def downgrade():
    op.drop_table("sandbox_credential_grants")
    op.drop_table("sandbox_credentials")
