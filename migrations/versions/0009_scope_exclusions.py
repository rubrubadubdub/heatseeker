"""Research scope geographic exclusions (ADR-0012).

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_scope") as batch:
        batch.add_column(sa.Column("exclude_codes", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("research_scope") as batch:
        batch.drop_column("exclude_codes")
