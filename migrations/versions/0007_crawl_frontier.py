"""Crawl frontier with purpose + lineage (M3, spec §11.6).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_frontier",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_definition_id",
            sa.String(36),
            sa.ForeignKey("source_definition.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("normalised_url", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("discovered_via", sa.String(30), nullable=False),
        sa.Column("discovery_rule", sa.String(200), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("parent_url", sa.String(2000), nullable=True),
        sa.Column("expected_content", sa.String(100), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(200), nullable=True),
        sa.Column(
            "document_id",
            sa.String(36),
            sa.ForeignKey("source_document.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_crawl_frontier_source_definition_id", "crawl_frontier", ["source_definition_id"]
    )
    op.create_index("ix_crawl_frontier_status", "crawl_frontier", ["status"])
    op.create_index(
        "uq_crawl_frontier_source_url",
        "crawl_frontier",
        ["source_definition_id", "normalised_url"],
        unique=True,
    )
    op.create_index(
        "ix_crawl_frontier_claim", "crawl_frontier", ["source_definition_id", "status", "priority"]
    )


def downgrade() -> None:
    op.drop_table("crawl_frontier")
