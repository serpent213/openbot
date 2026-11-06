"""Track whether a newly-created thread has received its one automatic rename.

Revision ID: 0013
Revises: 0012
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("auto_renamed", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Only threads created after this feature was deployed are eligible.
    op.execute(sa.table("threads", sa.column("auto_renamed", sa.Boolean())).update()
               .values(auto_renamed=True))


def downgrade() -> None:
    op.drop_column("threads", "auto_renamed")
