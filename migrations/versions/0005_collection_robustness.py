"""Grading, auto-deprecation, adaptive cadence, distillation (M2 robustness).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_definition") as batch:
        batch.add_column(
            sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("fetch_successes", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("docs_new", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(
            sa.Column("docs_unchanged", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("quality_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("quality_grade", sa.String(2), nullable=True))
        batch.add_column(sa.Column("graded_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("grade_detail", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("deprecated_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("deprecation_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("retry_after_until", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("next_collect_at", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "collect_interval_seconds", sa.Float(), nullable=False, server_default="86400"
            )
        )
    with op.batch_alter_table("source_document") as batch:
        batch.add_column(sa.Column("distilled_path", sa.String(500), nullable=True))
        batch.add_column(sa.Column("distilled_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source_document") as batch:
        batch.drop_column("distilled_chars")
        batch.drop_column("distilled_path")
    with op.batch_alter_table("source_definition") as batch:
        for column in (
            "collect_interval_seconds",
            "next_collect_at",
            "retry_after_until",
            "deprecation_reason",
            "deprecated_at",
            "grade_detail",
            "graded_at",
            "quality_grade",
            "quality_score",
            "docs_unchanged",
            "docs_new",
            "fetch_successes",
            "fetch_attempts",
        ):
            batch.drop_column(column)
