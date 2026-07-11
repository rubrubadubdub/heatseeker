"""Source registry, source documents, research scopes (M2).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_definition",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("source_category", sa.String(50), nullable=False),
        sa.Column("base_url", sa.String(1000), nullable=True),
        sa.Column("jurisdiction", sa.String(100), nullable=True),
        sa.Column("geo_codes", sa.JSON(), nullable=True),
        sa.Column("access_method", sa.String(20), nullable=False),
        sa.Column("authority_tier", sa.Integer(), nullable=False),
        sa.Column("lifecycle_status", sa.String(20), nullable=False),
        sa.Column("robots_status", sa.String(20), nullable=False),
        sa.Column("robots_checked_at", sa.DateTime(), nullable=True),
        sa.Column("terms_status", sa.String(20), nullable=False),
        sa.Column("collection_scope", sa.JSON(), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("pack_id", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("health_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_source_definition_name", "source_definition", ["name"])
    op.create_index(
        "ix_source_definition_source_category", "source_definition", ["source_category"]
    )
    op.create_index(
        "ix_source_definition_lifecycle_status", "source_definition", ["lifecycle_status"]
    )

    op.create_table(
        "source_document",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_definition_id", sa.String(36), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("canonical_url", sa.String(2000), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("retrieval_count", sa.Integer(), nullable=False),
        sa.Column("claimed_published_at", sa.DateTime(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("raw_storage_path", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("language", sa.String(20), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("etag", sa.String(300), nullable=True),
        sa.Column("last_modified", sa.String(100), nullable=True),
        sa.Column("access_policy_snapshot", sa.JSON(), nullable=True),
        sa.Column("collector_version", sa.String(50), nullable=False),
        sa.Column("parser_version", sa.String(50), nullable=True),
    )
    op.create_index(
        "ix_source_document_source_definition_id", "source_document", ["source_definition_id"]
    )
    op.create_index("ix_source_document_content_hash", "source_document", ["content_hash"])
    op.create_index(
        "ix_source_document_dedupe", "source_document", ["source_definition_id", "content_hash"]
    )

    op.create_table(
        "research_scope",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("geo_codes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_research_scope_is_active", "research_scope", ["is_active"])


def downgrade() -> None:
    op.drop_table("research_scope")
    op.drop_table("source_document")
    op.drop_table("source_definition")
