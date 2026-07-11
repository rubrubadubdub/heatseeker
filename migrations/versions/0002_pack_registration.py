"""Industry pack registration table (M1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "industry_pack_registration",
        sa.Column("pack_id", sa.String(100), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("first_loaded_at", sa.DateTime(), nullable=False),
        sa.Column("loaded_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("industry_pack_registration")
