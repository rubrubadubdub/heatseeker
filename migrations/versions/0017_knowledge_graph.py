"""Projects, participation, relationships (M6 knowledge graph).

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("project_type_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("location_id", sa.String(36), nullable=True),
        sa.Column("geography_scope", sa.JSON(), nullable=True),
        sa.Column("estimated_value", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(10), nullable=True),
        sa.Column("start_date", sa.DateTime(), nullable=True),
        sa.Column("end_date", sa.DateTime(), nullable=True),
        sa.Column("expected_start_date", sa.DateTime(), nullable=True),
        sa.Column("expected_end_date", sa.DateTime(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sector_ids", sa.JSON(), nullable=False),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["location_id"], ["location.id"], name="fk_project_location_id_location"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_project_name_nonempty"),
        sa.CheckConstraint(
            "estimated_value IS NULL OR estimated_value >= 0",
            name="ck_project_value_nonnegative",
        ),
    )
    op.create_index("ix_project_name", "project", ["name"])
    op.create_index("ix_project_status", "project", ["status"])

    op.create_table(
        "project_participation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("role_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("contract_value", sa.Float(), nullable=True),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["project.id"], name="fk_project_participation_project_id_project"
        ),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_project_participation_organisation_id_organisation",
        ),
        sa.UniqueConstraint(
            "project_id",
            "organisation_id",
            "role_type",
            name="uq_project_participation_project_id",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_project_participation_confidence_range",
        ),
        sa.CheckConstraint(
            "contract_value IS NULL OR contract_value >= 0",
            name="ck_project_participation_value_nonnegative",
        ),
    )
    op.create_index(
        "ix_project_participation_project_id", "project_participation", ["project_id"]
    )
    op.create_index(
        "ix_project_participation_organisation_id",
        "project_participation",
        ["organisation_id"],
    )

    op.create_table(
        "relationship",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_entity_id", sa.String(36), nullable=False),
        sa.Column("object_entity_id", sa.String(36), nullable=False),
        sa.Column("relationship_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["organisation.id"],
            name="fk_relationship_subject_entity_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["object_entity_id"],
            ["organisation.id"],
            name="fk_relationship_object_entity_id_organisation",
        ),
        sa.CheckConstraint(
            "subject_entity_id != object_entity_id", name="ck_relationship_no_self_edge"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_relationship_confidence_range"
        ),
    )
    op.create_index("ix_relationship_subject_entity_id", "relationship", ["subject_entity_id"])
    op.create_index("ix_relationship_object_entity_id", "relationship", ["object_entity_id"])
    op.create_index("ix_relationship_relationship_type", "relationship", ["relationship_type"])
    op.create_index("ix_relationship_status", "relationship", ["status"])


def downgrade() -> None:
    op.drop_table("relationship")
    op.drop_table("project_participation")
    op.drop_table("project")
