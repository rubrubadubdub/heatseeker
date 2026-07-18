"""Offerings, account opportunities, suppression rules (M8 lead intelligence).

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "offering",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("pack_id", sa.String(100), nullable=True),
        sa.Column("target_archetype_ids", sa.JSON(), nullable=False),
        sa.Column("target_capability_ids", sa.JSON(), nullable=False),
        sa.Column("need_gap_capability_ids", sa.JSON(), nullable=False),
        sa.Column("negative_archetype_ids", sa.JSON(), nullable=False),
        sa.Column("geo_codes", sa.JSON(), nullable=False),
        sa.Column("scoring_weights", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_offering_name_nonempty"),
    )
    op.create_index("ix_offering_status", "offering", ["status"])

    op.create_table(
        "account_opportunity",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("offering_id", sa.String(36), nullable=False),
        sa.Column("fit_score", sa.Float(), nullable=False),
        sa.Column("timing_score", sa.Float(), nullable=False),
        sa.Column("evidence_quality_score", sa.Float(), nullable=False),
        sa.Column("accessibility_score", sa.Float(), nullable=False),
        sa.Column("relationship_score", sa.Float(), nullable=True),
        sa.Column("commercial_priority", sa.Float(), nullable=False),
        sa.Column("opportunity_stage", sa.String(20), nullable=False),
        sa.Column("component_scores", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("unknowns", sa.JSON(), nullable=False),
        sa.Column("next_action", sa.JSON(), nullable=True),
        sa.Column("owner", sa.String(100), nullable=True),
        sa.Column("rule_version", sa.String(50), nullable=False),
        sa.Column("scored_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_account_opportunity_organisation_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["offering_id"], ["offering.id"], name="fk_account_opportunity_offering_id_offering"
        ),
        sa.UniqueConstraint(
            "organisation_id", "offering_id", name="uq_account_opportunity_organisation_id"
        ),
        sa.CheckConstraint(
            "commercial_priority >= 0 AND commercial_priority <= 1",
            name="ck_account_opportunity_priority_range",
        ),
    )
    op.create_index(
        "ix_account_opportunity_organisation_id", "account_opportunity", ["organisation_id"]
    )
    op.create_index(
        "ix_account_opportunity_offering_id", "account_opportunity", ["offering_id"]
    )
    op.create_index(
        "ix_account_opportunity_commercial_priority",
        "account_opportunity",
        ["commercial_priority"],
    )
    op.create_index(
        "ix_account_opportunity_opportunity_stage",
        "account_opportunity",
        ["opportunity_stage"],
    )

    op.create_table(
        "suppression_rule",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("lifted_at", sa.DateTime(), nullable=True),
        sa.Column("lifted_by", sa.String(100), nullable=True),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_suppression_rule_organisation_id_organisation",
        ),
    )
    op.create_index(
        "ix_suppression_rule_organisation_id", "suppression_rule", ["organisation_id"]
    )
    op.create_index("ix_suppression_rule_active", "suppression_rule", ["active"])


def downgrade() -> None:
    op.drop_table("suppression_rule")
    op.drop_table("account_opportunity")
    op.drop_table("offering")
