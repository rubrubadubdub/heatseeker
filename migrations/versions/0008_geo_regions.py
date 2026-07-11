"""Named geography regions as data (ADR-0012).

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "geo_region",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("member_codes", sa.JSON(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(trim(code)) >= 3 AND length(code) <= 50", name="ck_geo_region_code_length"
        ),
        sa.CheckConstraint(
            "length(trim(name)) >= 1 AND length(name) <= 200", name="ck_geo_region_name_length"
        ),
    )


def downgrade() -> None:
    op.drop_table("geo_region")
