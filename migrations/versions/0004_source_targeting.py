"""Contextual source coverage, stable identities, and lineage.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-11
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id() -> str:
    return str(uuid.uuid4())


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug[:80] or "source"


def _canonical_url(value: str) -> str:
    """Conservative URL identity normalisation used only for legacy backfill."""
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, hostname, path, query, ""))


def _create_source_tables() -> None:
    op.create_table(
        "source_identity",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_definition_id", sa.String(36), nullable=False),
        sa.Column("identity_type", sa.String(50), nullable=False),
        sa.Column("identity_value", sa.String(2000), nullable=False),
        sa.Column("normalised_value", sa.String(2000), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definition.id"],
            name="fk_source_identity_source_definition_id_source_definition",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "identity_type",
            "normalised_value",
            name="uq_source_identity_type_normalised_value",
        ),
        sa.CheckConstraint(
            "length(trim(identity_type)) > 0",
            name="ck_source_identity_identity_type_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(normalised_value)) > 0",
            name="ck_source_identity_normalised_value_nonempty",
        ),
        sa.CheckConstraint(
            "length(identity_type) <= 50",
            name="ck_source_identity_identity_type_length",
        ),
        sa.CheckConstraint(
            "length(identity_value) <= 2000",
            name="ck_source_identity_identity_value_length",
        ),
        sa.CheckConstraint(
            "length(normalised_value) <= 2000",
            name="ck_source_identity_normalised_value_length",
        ),
    )
    op.create_index(
        "ix_source_identity_source_definition_id",
        "source_identity",
        ["source_definition_id"],
    )
    op.create_index(
        "uq_source_identity_one_primary",
        "source_identity",
        ["source_definition_id"],
        unique=True,
        sqlite_where=sa.text("is_primary = 1"),
    )

    op.create_table(
        "source_coverage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_definition_id", sa.String(36), nullable=False),
        sa.Column("coverage_key", sa.String(200), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("lifecycle_status", sa.String(20), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("relevance", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("authority_tier_override", sa.Integer(), nullable=True),
        sa.Column("collection_scope_override", sa.JSON(), nullable=True),
        sa.Column("parser_profile_override", sa.String(200), nullable=True),
        sa.Column("robots_status", sa.String(20), nullable=False),
        sa.Column("robots_checked_at", sa.DateTime(), nullable=True),
        sa.Column("origin", sa.String(50), nullable=False),
        sa.Column("origin_pack_id", sa.String(100), nullable=True),
        sa.Column("origin_pack_version", sa.String(100), nullable=True),
        sa.Column("origin_pack_hash", sa.String(64), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definition.id"],
            name="fk_source_coverage_source_definition_id_source_definition",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_definition_id",
            "coverage_key",
            name="uq_source_coverage_source_key",
        ),
        sa.UniqueConstraint(
            "id",
            "source_definition_id",
            name="uq_source_coverage_id_source_definition",
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'disabled')",
            name="ck_source_coverage_lifecycle_status",
        ),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_source_coverage_priority_range",
        ),
        sa.CheckConstraint(
            "relevance >= 0.0 AND relevance <= 1.0",
            name="ck_source_coverage_relevance_range",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_source_coverage_confidence_range",
        ),
        sa.CheckConstraint(
            "authority_tier_override IS NULL OR "
            "(authority_tier_override >= 1 AND authority_tier_override <= 7)",
            name="ck_source_coverage_authority_tier_override_range",
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
            name="ck_source_coverage_validity_range",
        ),
        sa.CheckConstraint(
            "length(trim(coverage_key)) > 0",
            name="ck_source_coverage_coverage_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(coverage_key) <= 200",
            name="ck_source_coverage_coverage_key_length",
        ),
        sa.CheckConstraint(
            "length(trim(name)) >= 1 AND length(name) <= 300",
            name="ck_source_coverage_name_length",
        ),
        sa.CheckConstraint(
            "origin <> 'pack_seed' OR "
            "(origin_pack_id IS NOT NULL AND origin_pack_version IS NOT NULL "
            "AND origin_pack_hash IS NOT NULL)",
            name="ck_source_coverage_pack_provenance_complete",
        ),
    )
    op.create_index(
        "ix_source_coverage_source_definition_id",
        "source_coverage",
        ["source_definition_id"],
    )
    op.create_index(
        "ix_source_coverage_lifecycle_status",
        "source_coverage",
        ["lifecycle_status"],
    )
    op.create_index(
        "ix_source_coverage_origin_pack_id",
        "source_coverage",
        ["origin_pack_id"],
    )

    op.create_table(
        "source_coverage_target",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_coverage_id", sa.String(36), nullable=False),
        sa.Column("dimension", sa.String(100), nullable=False),
        sa.Column("target_key", sa.String(300), nullable=False),
        sa.Column("target_label", sa.String(500), nullable=True),
        sa.Column("polarity", sa.String(20), nullable=False),
        sa.Column("match_mode", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_coverage_id"],
            ["source_coverage.id"],
            name="fk_source_coverage_target_source_coverage_id_source_coverage",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_coverage_id",
            "dimension",
            "target_key",
            name="uq_source_coverage_target_coverage_dimension_target",
        ),
        sa.CheckConstraint(
            "polarity IN ('include', 'exclude')",
            name="ck_source_coverage_target_polarity",
        ),
        sa.CheckConstraint(
            "match_mode IN ('exact', 'hierarchical', 'covers', 'within')",
            name="ck_source_coverage_target_match_mode",
        ),
        sa.CheckConstraint(
            "length(trim(dimension)) > 0",
            name="ck_source_coverage_target_dimension_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(target_key)) > 0",
            name="ck_source_coverage_target_target_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(dimension) <= 100",
            name="ck_source_coverage_target_dimension_length",
        ),
        sa.CheckConstraint(
            "length(target_key) <= 300",
            name="ck_source_coverage_target_target_key_length",
        ),
    )
    op.create_index(
        "ix_source_coverage_target_source_coverage_id",
        "source_coverage_target",
        ["source_coverage_id"],
    )
    op.create_index(
        "ix_source_coverage_target_lookup",
        "source_coverage_target",
        ["dimension", "target_key", "polarity"],
    )

    op.create_table(
        "source_relationship",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_definition_id", sa.String(36), nullable=False),
        sa.Column("related_source_definition_id", sa.String(36), nullable=False),
        sa.Column("relationship_type", sa.String(50), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("origin", sa.String(50), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definition.id"],
            name="fk_source_relationship_source_definition_id_source_definition",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["related_source_definition_id"],
            ["source_definition.id"],
            name="fk_source_relationship_related_source_definition_id_source_definition",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "source_definition_id",
            "related_source_definition_id",
            "relationship_type",
            name="uq_source_relationship_source_related_type",
        ),
        sa.CheckConstraint(
            "source_definition_id <> related_source_definition_id",
            name="ck_source_relationship_different_sources",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_source_relationship_confidence_range",
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
            name="ck_source_relationship_validity_range",
        ),
        sa.CheckConstraint(
            "length(trim(relationship_type)) > 0",
            name="ck_source_relationship_relationship_type_nonempty",
        ),
    )
    op.create_index(
        "ix_source_relationship_source_definition_id",
        "source_relationship",
        ["source_definition_id"],
    )
    op.create_index(
        "ix_source_relationship_related_source_definition_id",
        "source_relationship",
        ["related_source_definition_id"],
    )
    op.create_index(
        "ix_source_relationship_relationship_type",
        "source_relationship",
        ["relationship_type"],
    )


def _backfill_source_targeting() -> None:
    connection = op.get_bind()
    now = connection.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar_one()
    sources = connection.execute(
        sa.text(
            "SELECT id, name, base_url, pack_id, geo_codes "
            "FROM source_definition ORDER BY created_at, id"
        )
    ).mappings()
    used_urls: set[str] = set()
    for source in sources:
        source_id = source["id"]
        pack_id = source["pack_id"]
        seed_slug = _slug(source["name"])
        identities: list[tuple[str, str, str, bool]] = []
        if pack_id:
            value = f"{pack_id}:{seed_slug}"
            identities.append(("pack_seed", value, value.lower(), True))
        if source["base_url"]:
            canonical = _canonical_url(source["base_url"])
            if canonical and canonical not in used_urls:
                identities.append(("url", source["base_url"], canonical, not identities))
                used_urls.add(canonical)
        for identity_type, identity_value, normalised, is_primary in identities:
            connection.execute(
                sa.text(
                    "INSERT INTO source_identity "
                    "(id, source_definition_id, identity_type, identity_value, "
                    "normalised_value, is_primary, origin, created_at, updated_at) "
                    "VALUES (:id, :source_id, :identity_type, :identity_value, "
                    ":normalised, :is_primary, 'migration', :now, :now)"
                ),
                {
                    "id": _id(),
                    "source_id": source_id,
                    "identity_type": identity_type,
                    "identity_value": identity_value,
                    "normalised": normalised,
                    "is_primary": is_primary,
                    "now": now,
                },
            )

        coverage_id = _id()
        coverage_key = (
            f"pack:{_slug(pack_id)}:{seed_slug}:default" if pack_id else f"legacy:{source_id}"
        )
        connection.execute(
            sa.text(
                "INSERT INTO source_coverage "
                "(id, source_definition_id, coverage_key, name, description, "
                "lifecycle_status, priority, relevance, confidence, "
                "authority_tier_override, collection_scope_override, "
                "parser_profile_override, robots_status, robots_checked_at, origin, "
                "origin_pack_id, origin_pack_version, "
                "origin_pack_hash, valid_from, valid_to, created_at, updated_at) "
                "VALUES (:id, :source_id, :coverage_key, :name, NULL, 'active', 50, "
                "1.0, 1.0, NULL, NULL, NULL, 'unknown', NULL, 'migration', :pack_id, NULL, NULL, "
                "NULL, NULL, :now, :now)"
            ),
            {
                "id": coverage_id,
                "source_id": source_id,
                "coverage_key": coverage_key,
                "name": source["name"],
                "pack_id": pack_id,
                "now": now,
            },
        )
        targets: list[tuple[str, str, str]] = []
        if pack_id:
            targets.append(("industry", _slug(pack_id), "exact"))
        raw_geo = source["geo_codes"] or []
        if isinstance(raw_geo, str):
            # Some SQLite drivers expose JSON as text during a raw migration query.
            import json

            try:
                raw_geo = json.loads(raw_geo)
            except json.JSONDecodeError:
                raw_geo = []
        seen_geo: set[str] = set()
        for raw_code in raw_geo:
            code = str(raw_code).strip().upper().replace(" ", "_")
            if code and code not in seen_geo:
                seen_geo.add(code)
                targets.append(("region", code, "hierarchical"))
        for dimension, target_key, match_mode in targets:
            connection.execute(
                sa.text(
                    "INSERT INTO source_coverage_target "
                    "(id, source_coverage_id, dimension, target_key, target_label, "
                    "polarity, match_mode, created_at, updated_at) "
                    "VALUES (:id, :coverage_id, :dimension, :target_key, NULL, "
                    "'include', :match_mode, :now, :now)"
                ),
                {
                    "id": _id(),
                    "coverage_id": coverage_id,
                    "dimension": dimension,
                    "target_key": target_key,
                    "match_mode": match_mode,
                    "now": now,
                },
            )


def upgrade() -> None:
    with op.batch_alter_table("source_definition") as batch:
        batch.add_column(sa.Column("language", sa.String(35), nullable=True))
        batch.add_column(sa.Column("expected_update_frequency", sa.String(100), nullable=True))
        batch.add_column(sa.Column("authentication_type", sa.String(50), nullable=True))
        batch.add_column(sa.Column("rate_limit_policy", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("parser_profile", sa.String(200), nullable=True))
        batch.create_check_constraint(
            "ck_source_definition_name_length",
            "length(trim(name)) >= 1 AND length(name) <= 300",
        )
        batch.create_check_constraint(
            "ck_source_definition_source_category_length",
            "length(trim(source_category)) >= 1 AND length(source_category) <= 50",
        )
        batch.create_check_constraint(
            "ck_source_definition_base_url_length",
            "base_url IS NULL OR length(base_url) <= 1000",
        )
        batch.create_check_constraint(
            "ck_source_definition_authority_tier_range",
            "authority_tier >= 1 AND authority_tier <= 7",
        )
        batch.create_check_constraint(
            "ck_source_definition_consecutive_failures_nonnegative",
            "consecutive_failures >= 0",
        )
        batch.create_check_constraint(
            "ck_source_definition_health_score_range",
            "health_score IS NULL OR (health_score >= 0.0 AND health_score <= 1.0)",
        )
        batch.create_check_constraint(
            "ck_source_definition_access_method",
            "access_method IN ('api','bulk','rss','sitemap','html','rendered','manual')",
        )
        batch.create_check_constraint(
            "ck_source_definition_lifecycle_status",
            "lifecycle_status IN "
            "('proposed','candidate','active','degraded','disabled','rejected')",
        )
        batch.create_check_constraint(
            "ck_source_definition_robots_status",
            "robots_status IN ('unknown','allowed','disallowed','unreachable','not_applicable')",
        )
        batch.create_check_constraint(
            "ck_source_definition_terms_status",
            "terms_status IN ('unreviewed','approved','unclear','prohibited')",
        )

    _create_source_tables()

    with op.batch_alter_table("research_scope") as batch:
        batch.add_column(
            sa.Column("industry_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("target_filters", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )
        batch.add_column(
            sa.Column("include_unknown", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    connection = op.get_bind()
    active_ids = list(
        connection.execute(
            sa.text("SELECT id FROM research_scope WHERE is_active = 1 ORDER BY created_at, id")
        ).scalars()
    )
    for duplicate_id in active_ids[1:]:
        connection.execute(
            sa.text("UPDATE research_scope SET is_active = 0 WHERE id = :id"),
            {"id": duplicate_id},
        )
    op.create_index(
        "uq_research_scope_one_active",
        "research_scope",
        ["is_active"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    _backfill_source_targeting()

    op.drop_index("ix_source_document_dedupe", table_name="source_document")
    with op.batch_alter_table("source_document") as batch:
        batch.add_column(sa.Column("source_coverage_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("targeting_snapshot", sa.JSON(), nullable=True))
        batch.create_foreign_key(
            "fk_source_document_source_definition_id_source_definition",
            "source_definition",
            ["source_definition_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_source_document_source_coverage_id_source_coverage",
            "source_coverage",
            ["source_coverage_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_source_document_coverage_source",
            "source_coverage",
            ["source_coverage_id", "source_definition_id"],
            ["id", "source_definition_id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_source_document_source_url_content_hash",
            ["source_definition_id", "source_url", "content_hash"],
        )
        batch.create_check_constraint(
            "ck_source_document_retrieval_count_positive", "retrieval_count >= 1"
        )
        batch.create_check_constraint(
            "ck_source_document_size_bytes_nonnegative", "size_bytes >= 0"
        )
        batch.create_check_constraint(
            "ck_source_document_content_hash_length", "length(content_hash) = 64"
        )
        batch.create_check_constraint(
            "ck_source_document_http_status_range",
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
        )
    op.create_index(
        "ix_source_document_source_coverage_id",
        "source_document",
        ["source_coverage_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_document_source_coverage_id", table_name="source_document")
    with op.batch_alter_table("source_document") as batch:
        batch.drop_constraint("ck_source_document_http_status_range", type_="check")
        batch.drop_constraint("ck_source_document_content_hash_length", type_="check")
        batch.drop_constraint("ck_source_document_size_bytes_nonnegative", type_="check")
        batch.drop_constraint("ck_source_document_retrieval_count_positive", type_="check")
        batch.drop_constraint("uq_source_document_source_url_content_hash", type_="unique")
        batch.drop_constraint("fk_source_document_coverage_source", type_="foreignkey")
        batch.drop_constraint(
            "fk_source_document_source_coverage_id_source_coverage", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_source_document_source_definition_id_source_definition", type_="foreignkey"
        )
        batch.drop_column("targeting_snapshot")
        batch.drop_column("source_coverage_id")
    op.create_index(
        "ix_source_document_dedupe",
        "source_document",
        ["source_definition_id", "content_hash"],
    )

    op.drop_index("uq_research_scope_one_active", table_name="research_scope")
    with op.batch_alter_table("research_scope") as batch:
        batch.drop_column("include_unknown")
        batch.drop_column("target_filters")
        batch.drop_column("industry_ids")

    op.drop_table("source_relationship")
    op.drop_table("source_coverage_target")
    op.drop_table("source_coverage")
    op.drop_table("source_identity")

    with op.batch_alter_table("source_definition") as batch:
        batch.drop_constraint("ck_source_definition_base_url_length", type_="check")
        batch.drop_constraint("ck_source_definition_source_category_length", type_="check")
        batch.drop_constraint("ck_source_definition_name_length", type_="check")
        batch.drop_constraint("ck_source_definition_terms_status", type_="check")
        batch.drop_constraint("ck_source_definition_robots_status", type_="check")
        batch.drop_constraint("ck_source_definition_lifecycle_status", type_="check")
        batch.drop_constraint("ck_source_definition_access_method", type_="check")
        batch.drop_constraint("ck_source_definition_health_score_range", type_="check")
        batch.drop_constraint(
            "ck_source_definition_consecutive_failures_nonnegative", type_="check"
        )
        batch.drop_constraint("ck_source_definition_authority_tier_range", type_="check")
        batch.drop_column("parser_profile")
        batch.drop_column("rate_limit_policy")
        batch.drop_column("authentication_type")
        batch.drop_column("expected_update_frequency")
        batch.drop_column("language")
