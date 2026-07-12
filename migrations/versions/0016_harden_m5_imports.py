"""Snapshot M5 import scope and declared source authority.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("observation") as batch_op:
        batch_op.add_column(
            sa.Column("human_verified", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("verified_by", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("verified_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("bulk_import_run") as batch_op:
        batch_op.add_column(sa.Column("scope_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("pack_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("authority_tier", sa.Integer(), nullable=False, server_default="5")
        )
        batch_op.create_check_constraint(
            "authority_tier_range", "authority_tier >= 1 AND authority_tier <= 7"
        )


def downgrade() -> None:
    with op.batch_alter_table("bulk_import_run") as batch_op:
        batch_op.drop_constraint("authority_tier_range", type_="check")
        batch_op.drop_column("authority_tier")
        batch_op.drop_column("pack_snapshot")
        batch_op.drop_column("scope_snapshot")

    with op.batch_alter_table("observation") as batch_op:
        batch_op.drop_column("verified_at")
        batch_op.drop_column("verified_by")
        batch_op.drop_column("human_verified")
