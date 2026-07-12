"""Refine entity matching priority and reversible-merge audit state.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("entity_match_candidate") as batch_op:
        batch_op.add_column(
            sa.Column(
                "commercial_importance", sa.Float(), nullable=False, server_default="0.0"
            )
        )
        batch_op.add_column(
            sa.Column("downstream_impact", sa.Float(), nullable=False, server_default="0.0")
        )
        batch_op.add_column(
            sa.Column("ease_of_resolution", sa.Float(), nullable=False, server_default="0.0")
        )
        batch_op.add_column(
            sa.Column("priority_score", sa.Float(), nullable=False, server_default="0.0")
        )
        batch_op.create_index(
            "ix_entity_match_candidate_priority_score", ["priority_score"], unique=False
        )

    # Existing queues retain their probability ordering until their next scan computes
    # all dimensions. This is deterministic and does not invent commercial importance.
    op.execute("UPDATE entity_match_candidate SET priority_score = score")

    with op.batch_alter_table("entity_merge") as batch_op:
        batch_op.add_column(sa.Column("candidate_prior_match_state", sa.String(30)))
        batch_op.add_column(sa.Column("candidate_prior_resolution", sa.String(20)))
        batch_op.add_column(sa.Column("candidate_prior_resolved_by", sa.String(100)))
        batch_op.add_column(sa.Column("candidate_prior_resolved_at", sa.DateTime()))
        batch_op.add_column(sa.Column("candidate_prior_notes", sa.Text()))
        batch_op.add_column(sa.Column("candidate_prior_updated_at", sa.DateTime()))
        batch_op.add_column(sa.Column("reversed_by", sa.String(100)))


def downgrade() -> None:
    with op.batch_alter_table("entity_merge") as batch_op:
        batch_op.drop_column("reversed_by")
        batch_op.drop_column("candidate_prior_updated_at")
        batch_op.drop_column("candidate_prior_notes")
        batch_op.drop_column("candidate_prior_resolved_at")
        batch_op.drop_column("candidate_prior_resolved_by")
        batch_op.drop_column("candidate_prior_resolution")
        batch_op.drop_column("candidate_prior_match_state")

    with op.batch_alter_table("entity_match_candidate") as batch_op:
        batch_op.drop_index("ix_entity_match_candidate_priority_score")
        batch_op.drop_column("priority_score")
        batch_op.drop_column("ease_of_resolution")
        batch_op.drop_column("downstream_impact")
        batch_op.drop_column("commercial_importance")
