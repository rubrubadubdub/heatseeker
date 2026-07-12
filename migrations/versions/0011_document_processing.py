"""Versioned document processing and discovered-reference provenance.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_document") as batch:
        batch.add_column(sa.Column("detected_content_type", sa.String(200), nullable=True))
        batch.add_column(sa.Column("content_disposition", sa.String(1000), nullable=True))
        batch.add_column(sa.Column("original_filename", sa.String(500), nullable=True))

    op.create_table(
        "document_processing_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_document_id", sa.String(36), nullable=False),
        sa.Column("pipeline_version", sa.String(100), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detected_content_type", sa.String(200), nullable=True),
        sa.Column("filename", sa.String(500), nullable=True),
        sa.Column("extraction_method", sa.String(100), nullable=True),
        sa.Column("manifest_path", sa.String(500), nullable=True),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("text_path", sa.String(500), nullable=True),
        sa.Column("text_hash", sa.String(64), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            name="fk_document_processing_run_source_document_id_source_document",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "pipeline_version",
            "config_hash",
            name="uq_document_processing_run_document_pipeline_config",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded','partial','unsupported','quarantined',"
            "'corrupt','encrypted','failed')",
            name="ck_document_processing_run_status",
        ),
        sa.CheckConstraint(
            "length(trim(pipeline_version)) > 0",
            name="ck_document_processing_run_pipeline_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_document_processing_run_config_hash_length",
        ),
        sa.CheckConstraint(
            "manifest_hash IS NULL OR length(manifest_hash) = 64",
            name="ck_document_processing_run_manifest_hash_length",
        ),
        sa.CheckConstraint(
            "text_hash IS NULL OR length(text_hash) = 64",
            name="ck_document_processing_run_text_hash_length",
        ),
        sa.CheckConstraint(
            "text_chars IS NULL OR text_chars >= 0",
            name="ck_document_processing_run_text_chars_nonnegative",
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_document_processing_run_page_count_nonnegative",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="ck_document_processing_run_time_order",
        ),
    )
    op.create_index(
        "ix_document_processing_run_document_status_created",
        "document_processing_run",
        ["source_document_id", "status", "created_at"],
    )
    op.create_index("ix_document_processing_run_status", "document_processing_run", ["status"])

    op.create_table(
        "source_document_reference",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parent_document_id", sa.String(36), nullable=False),
        sa.Column("child_document_id", sa.String(36), nullable=True),
        sa.Column("target_url", sa.String(2000), nullable=False),
        sa.Column("normalised_url", sa.String(2000), nullable=False),
        sa.Column("reference_kind", sa.String(50), nullable=False),
        sa.Column("discovery_rule", sa.String(200), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("extractor_version", sa.String(100), nullable=False),
        sa.Column("decision", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_document_id"],
            ["source_document.id"],
            name="fk_source_document_reference_parent_document_id_source_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["child_document_id"],
            ["source_document.id"],
            name="fk_source_document_reference_child_document_id_source_document",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "parent_document_id",
            "extractor_version",
            "reference_kind",
            "ordinal",
            name="uq_source_document_reference_occurrence",
        ),
        sa.CheckConstraint(
            "child_document_id IS NULL OR child_document_id <> parent_document_id",
            name="ck_source_document_reference_different_documents",
        ),
        sa.CheckConstraint(
            "length(trim(target_url)) > 0",
            name="ck_source_document_reference_target_url_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(normalised_url)) > 0",
            name="ck_source_document_reference_normalised_url_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(reference_kind)) > 0",
            name="ck_source_document_reference_reference_kind_nonempty",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_source_document_reference_ordinal_nonnegative",
        ),
        sa.CheckConstraint(
            "length(trim(extractor_version)) > 0",
            name="ck_source_document_reference_extractor_version_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(decision)) > 0",
            name="ck_source_document_reference_decision_nonempty",
        ),
    )
    op.create_index(
        "ix_source_document_reference_parent_kind_decision",
        "source_document_reference",
        ["parent_document_id", "reference_kind", "decision"],
    )
    op.create_index(
        "ix_source_document_reference_child_document_id",
        "source_document_reference",
        ["child_document_id"],
    )
    op.create_index(
        "ix_source_document_reference_normalised_url_decision",
        "source_document_reference",
        ["normalised_url", "decision"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_document_reference_normalised_url_decision",
        table_name="source_document_reference",
    )
    op.drop_index(
        "ix_source_document_reference_child_document_id",
        table_name="source_document_reference",
    )
    op.drop_index(
        "ix_source_document_reference_parent_kind_decision",
        table_name="source_document_reference",
    )
    op.drop_table("source_document_reference")

    op.drop_index("ix_document_processing_run_status", table_name="document_processing_run")
    op.drop_index(
        "ix_document_processing_run_document_status_created",
        table_name="document_processing_run",
    )
    op.drop_table("document_processing_run")

    with op.batch_alter_table("source_document") as batch:
        batch.drop_column("original_filename")
        batch.drop_column("content_disposition")
        batch.drop_column("detected_content_type")
