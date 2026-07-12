"""Evidence chain and profile intelligence (M5): observations, fact assertions,
classifications, capabilities, size estimates, research questions, bulk imports.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_entity_id", sa.String(36), nullable=True),
        sa.Column("predicate", sa.String(100), nullable=False),
        sa.Column("object_value", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("source_document_id", sa.String(36), nullable=False),
        sa.Column("source_location", sa.JSON(), nullable=True),
        sa.Column("extraction_method", sa.String(20), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=False),
        sa.Column("normalisation_status", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["organisation.id"],
            name="fk_observation_subject_entity_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            name="fk_observation_source_document_id_source_document",
        ),
        sa.CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="ck_observation_extraction_confidence_range",
        ),
    )
    op.create_index("ix_observation_subject_entity_id", "observation", ["subject_entity_id"])
    op.create_index("ix_observation_predicate", "observation", ["predicate"])
    op.create_index("ix_observation_source_document_id", "observation", ["source_document_id"])
    op.create_index(
        "ix_observation_subject_predicate", "observation", ["subject_entity_id", "predicate"]
    )

    op.create_table(
        "fact_assertion",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject_entity_id", sa.String(36), nullable=False),
        sa.Column("predicate", sa.String(100), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("authority_score", sa.Float(), nullable=False),
        sa.Column("extraction_score", sa.Float(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("freshness_score", sa.Float(), nullable=False),
        sa.Column("corroboration_score", sa.Float(), nullable=False),
        sa.Column("contradiction_score", sa.Float(), nullable=False),
        sa.Column("final_confidence", sa.Float(), nullable=False),
        sa.Column("confidence_vocabulary", sa.String(20), nullable=False),
        sa.Column("supporting_observation_ids", sa.JSON(), nullable=False),
        sa.Column("contradicting_observation_ids", sa.JSON(), nullable=False),
        sa.Column("independent_source_count", sa.Integer(), nullable=False),
        sa.Column("best_evidence_document_id", sa.String(36), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(), nullable=True),
        sa.Column("rule_version", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["subject_entity_id"],
            ["organisation.id"],
            name="fk_fact_assertion_subject_entity_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["best_evidence_document_id"],
            ["source_document.id"],
            name="fk_fact_assertion_best_evidence_document_id_source_document",
        ),
        sa.UniqueConstraint(
            "subject_entity_id", "predicate", name="uq_fact_assertion_subject_entity_id"
        ),
        sa.CheckConstraint(
            "final_confidence >= 0 AND final_confidence <= 1",
            name="ck_fact_assertion_final_confidence_range",
        ),
    )
    op.create_index(
        "ix_fact_assertion_subject_entity_id", "fact_assertion", ["subject_entity_id"]
    )
    op.create_index("ix_fact_assertion_predicate", "fact_assertion", ["predicate"])
    op.create_index("ix_fact_assertion_status", "fact_assertion", ["status"])

    op.create_table(
        "classification_assignment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("pack_id", sa.String(100), nullable=False),
        sa.Column("taxonomy_id", sa.String(100), nullable=False),
        sa.Column("category_id", sa.String(200), nullable=False),
        sa.Column("category_label", sa.String(300), nullable=False),
        sa.Column("assignment_type", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("classifier_version", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["organisation.id"],
            name="fk_classification_assignment_entity_id_organisation",
        ),
        sa.UniqueConstraint(
            "entity_id",
            "pack_id",
            "taxonomy_id",
            "category_id",
            name="uq_classification_assignment_entity_id",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_classification_assignment_confidence_range",
        ),
    )
    op.create_index(
        "ix_classification_assignment_entity_id", "classification_assignment", ["entity_id"]
    )

    op.create_table(
        "capability_assignment",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("pack_id", sa.String(100), nullable=False),
        sa.Column("capability_id", sa.String(200), nullable=False),
        sa.Column("capability_label", sa.String(300), nullable=False),
        sa.Column("capability_status", sa.String(30), nullable=False),
        sa.Column("evidence_strength", sa.Float(), nullable=False),
        sa.Column("recency_score", sa.Float(), nullable=False),
        sa.Column("geographic_scope", sa.JSON(), nullable=True),
        sa.Column("scale_indicator", sa.JSON(), nullable=True),
        sa.Column("evidence_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_capability_assignment_organisation_id_organisation",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "pack_id",
            "capability_id",
            name="uq_capability_assignment_organisation_id",
        ),
        sa.CheckConstraint(
            "evidence_strength >= 0 AND evidence_strength <= 1",
            name="ck_capability_assignment_evidence_strength_range",
        ),
    )
    op.create_index(
        "ix_capability_assignment_organisation_id", "capability_assignment", ["organisation_id"]
    )
    op.create_index(
        "ix_capability_assignment_capability_status",
        "capability_assignment",
        ["capability_status"],
    )

    op.create_table(
        "size_estimate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("concept", sa.String(40), nullable=False),
        sa.Column("band", sa.String(100), nullable=False),
        sa.Column("basis", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rule_version", sa.String(50), nullable=False),
        sa.Column("estimated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_size_estimate_organisation_id_organisation",
        ),
        sa.UniqueConstraint(
            "organisation_id", "concept", name="uq_size_estimate_organisation_id"
        ),
    )
    op.create_index("ix_size_estimate_organisation_id", "size_estimate", ["organisation_id"])

    op.create_table(
        "research_question",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_id", sa.String(36), nullable=True),
        sa.Column("question_type", sa.String(100), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("priority", sa.Float(), nullable=False),
        sa.Column("generated_by", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["organisation.id"], name="fk_research_question_entity_id_organisation"
        ),
    )
    op.create_index("ix_research_question_entity_id", "research_question", ["entity_id"])
    op.create_index("ix_research_question_question_type", "research_question", ["question_type"])
    op.create_index("ix_research_question_status", "research_question", ["status"])

    op.create_table(
        "bulk_import_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dataset_name", sa.String(300), nullable=False),
        sa.Column("publisher", sa.String(300), nullable=True),
        sa.Column("dataset_version", sa.String(100), nullable=True),
        sa.Column("coverage_date", sa.String(50), nullable=True),
        sa.Column("licence_note", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("source_definition_id", sa.String(36), nullable=True),
        sa.Column("source_document_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("matched_existing_count", sa.Integer(), nullable=False),
        sa.Column("skipped_out_of_scope_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("rejected_samples", sa.JSON(), nullable=False),
        sa.Column("transformation_version", sa.String(50), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definition.id"],
            name="fk_bulk_import_run_source_definition_id_source_definition",
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            name="fk_bulk_import_run_source_document_id_source_document",
        ),
    )
    op.create_index("ix_bulk_import_run_checksum", "bulk_import_run", ["checksum"])
    op.create_index("ix_bulk_import_run_status", "bulk_import_run", ["status"])


def downgrade() -> None:
    op.drop_table("bulk_import_run")
    op.drop_table("research_question")
    op.drop_table("size_estimate")
    op.drop_table("capability_assignment")
    op.drop_table("classification_assignment")
    op.drop_table("fact_assertion")
    op.drop_table("observation")
