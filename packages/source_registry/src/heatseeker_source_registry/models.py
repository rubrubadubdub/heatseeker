"""Source registry + raw evidence tables (spec §10.2, §13.13) and research scopes."""

import enum
import uuid
from datetime import datetime

from heatseeker_common.db import Base, UTCDateTime
from heatseeker_common.timeutil import utc_now
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class SourceLifecycle(enum.StrEnum):
    PROPOSED = "proposed"  # suggested (AI/system) — needs review
    CANDIDATE = "candidate"  # accepted for vetting — policy not yet cleared
    ACTIVE = "active"  # collectable
    DEGRADED = "degraded"  # repeated failures — retried cautiously
    DISABLED = "disabled"  # manually switched off
    DEPRECATED = "deprecated"  # auto-retired by the evaluator (reason recorded, reversible)
    REJECTED = "rejected"  # reviewed and refused (kept for audit)


class RobotsStatus(enum.StrEnum):
    UNKNOWN = "unknown"
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    UNREACHABLE = "unreachable"
    NOT_APPLICABLE = "not_applicable"  # manual/API-key sources


class TermsStatus(enum.StrEnum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    UNCLEAR = "unclear"
    PROHIBITED = "prohibited"


class SourceCoverageLifecycle(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SourceTargetPolarity(enum.StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


class SourceTargetMatchMode(enum.StrEnum):
    EXACT = "exact"
    HIERARCHICAL = "hierarchical"
    COVERS = "covers"
    WITHIN = "within"


class SourceDefinition(Base):
    __tablename__ = "source_definition"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(300))
    source_category: Mapped[str] = mapped_column(String(50), index=True)
    base_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(100), nullable=True)
    geo_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)  # normalised
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    access_method: Mapped[str] = mapped_column(String(20))  # api|bulk|rss|sitemap|html|manual
    authority_tier: Mapped[int] = mapped_column(Integer, default=5)
    expected_update_frequency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default=SourceLifecycle.CANDIDATE, index=True
    )
    robots_status: Mapped[str] = mapped_column(String(20), default=RobotsStatus.UNKNOWN)
    robots_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Per-source robots override (ADR-0013): NULL = follow settings.robots_policy;
    # True = always honour robots here; False = always ignore robots here.
    respect_robots_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    terms_status: Mapped[str] = mapped_column(String(20), default=TermsStatus.UNREVIEWED)
    authentication_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rate_limit_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    collection_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parser_profile: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="user")  # pack_seed|user|proposal
    pack_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    health_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Vetting/grading counters + verdict (grading.py). Ungraded until evidence exists.
    fetch_attempts: Mapped[int] = mapped_column(Integer, default=0)
    fetch_successes: Mapped[int] = mapped_column(Integer, default=0)
    docs_new: Mapped[int] = mapped_column(Integer, default=0)
    docs_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    graded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    grade_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Auto-deprecation (reversible; reason always recorded and audited)
    deprecated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deprecation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Adaptive collection cadence + throttle compliance (schedule.py)
    retry_after_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_collect_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    collect_interval_seconds: Mapped[float] = mapped_column(Float, default=86400.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    identities: Mapped[list["SourceIdentity"]] = relationship(
        back_populates="source_definition", cascade="all, delete-orphan", passive_deletes=True
    )
    coverages: Mapped[list["SourceCoverage"]] = relationship(
        back_populates="source_definition", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list["SourceDocument"]] = relationship(
        back_populates="source_definition", passive_deletes=True
    )
    outbound_relationships: Mapped[list["SourceRelationship"]] = relationship(
        back_populates="source_definition",
        cascade="all, delete-orphan",
        foreign_keys="SourceRelationship.source_definition_id",
        passive_deletes=True,
    )
    inbound_relationships: Mapped[list["SourceRelationship"]] = relationship(
        back_populates="related_source_definition",
        cascade="all, delete-orphan",
        foreign_keys="SourceRelationship.related_source_definition_id",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) >= 1 AND length(name) <= 300", name="name_length"),
        CheckConstraint(
            "length(trim(source_category)) >= 1 AND length(source_category) <= 50",
            name="source_category_length",
        ),
        CheckConstraint("base_url IS NULL OR length(base_url) <= 1000", name="base_url_length"),
        CheckConstraint("authority_tier >= 1 AND authority_tier <= 7", name="authority_tier_range"),
        CheckConstraint("consecutive_failures >= 0", name="consecutive_failures_nonnegative"),
        CheckConstraint(
            "health_score IS NULL OR (health_score >= 0.0 AND health_score <= 1.0)",
            name="health_score_range",
        ),
        CheckConstraint(
            "access_method IN ('api','bulk','rss','sitemap','html','rendered','manual')",
            name="access_method",
        ),
        CheckConstraint(
            "lifecycle_status IN "
            "('proposed','candidate','active','degraded','disabled','deprecated','rejected')",
            name="lifecycle_status",
        ),
        CheckConstraint(
            "robots_status IN ('unknown','allowed','disallowed','unreachable','not_applicable')",
            name="robots_status",
        ),
        CheckConstraint(
            "terms_status IN ('unreviewed','approved','unclear','prohibited')",
            name="terms_status",
        ),
        Index("ix_source_definition_name", "name"),
    )


class SourceIdentity(Base):
    """A stable, globally de-duplicated identity or alias for a source."""

    __tablename__ = "source_identity"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="CASCADE"), nullable=False
    )
    identity_type: Mapped[str] = mapped_column(String(50))
    identity_value: Mapped[str] = mapped_column(String(2000))
    normalised_value: Mapped[str] = mapped_column(String(2000))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(50), default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    source_definition: Mapped["SourceDefinition"] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint(
            "identity_type",
            "normalised_value",
            name="uq_source_identity_type_normalised_value",
        ),
        CheckConstraint("length(trim(identity_type)) > 0", name="identity_type_nonempty"),
        CheckConstraint("length(trim(normalised_value)) > 0", name="normalised_value_nonempty"),
        CheckConstraint("length(identity_type) <= 50", name="identity_type_length"),
        CheckConstraint("length(identity_value) <= 2000", name="identity_value_length"),
        CheckConstraint("length(normalised_value) <= 2000", name="normalised_value_length"),
        Index("ix_source_identity_source_definition_id", "source_definition_id"),
        Index(
            "uq_source_identity_one_primary",
            "source_definition_id",
            unique=True,
            sqlite_where=text("is_primary = 1"),
        ),
    )


class SourceCoverage(Base):
    """One coherent applicability tuple for a canonical source definition."""

    __tablename__ = "source_coverage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="CASCADE"), nullable=False
    )
    coverage_key: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(20), default=SourceCoverageLifecycle.ACTIVE
    )
    priority: Mapped[int] = mapped_column(Integer, default=50)
    relevance: Mapped[float] = mapped_column(Float, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    authority_tier_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collection_scope_override: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parser_profile_override: Mapped[str | None] = mapped_column(String(200), nullable=True)
    robots_status: Mapped[str] = mapped_column(String(20), default=RobotsStatus.UNKNOWN)
    robots_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    origin: Mapped[str] = mapped_column(String(50), default="user")
    origin_pack_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    origin_pack_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    origin_pack_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    source_definition: Mapped["SourceDefinition"] = relationship(back_populates="coverages")
    targets: Mapped[list["SourceCoverageTarget"]] = relationship(
        back_populates="coverage", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list["SourceDocument"]] = relationship(
        back_populates="coverage",
        foreign_keys="SourceDocument.source_coverage_id",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "source_definition_id", "coverage_key", name="uq_source_coverage_source_key"
        ),
        UniqueConstraint(
            "id",
            "source_definition_id",
            name="uq_source_coverage_id_source_definition",
        ),
        CheckConstraint("lifecycle_status IN ('active', 'disabled')", name="lifecycle_status"),
        CheckConstraint("priority >= 0 AND priority <= 100", name="priority_range"),
        CheckConstraint("relevance >= 0.0 AND relevance <= 1.0", name="relevance_range"),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "authority_tier_override IS NULL "
            "OR (authority_tier_override >= 1 AND authority_tier_override <= 7)",
            name="authority_tier_override_range",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
            name="validity_range",
        ),
        CheckConstraint("length(trim(coverage_key)) > 0", name="coverage_key_nonempty"),
        CheckConstraint("length(coverage_key) <= 200", name="coverage_key_length"),
        CheckConstraint("length(trim(name)) >= 1 AND length(name) <= 300", name="name_length"),
        CheckConstraint(
            "origin <> 'pack_seed' OR "
            "(origin_pack_id IS NOT NULL AND origin_pack_version IS NOT NULL "
            "AND origin_pack_hash IS NOT NULL)",
            name="pack_provenance_complete",
        ),
        Index("ix_source_coverage_source_definition_id", "source_definition_id"),
        Index("ix_source_coverage_lifecycle_status", "lifecycle_status"),
        Index("ix_source_coverage_origin_pack_id", "origin_pack_id"),
    )


class SourceCoverageTarget(Base):
    """A target within a coverage profile; dimensions remain taxonomy-extensible."""

    __tablename__ = "source_coverage_target"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_coverage_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_coverage.id", ondelete="CASCADE"), nullable=False
    )
    dimension: Mapped[str] = mapped_column(String(100))
    target_key: Mapped[str] = mapped_column(String(300))
    target_label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    polarity: Mapped[str] = mapped_column(String(20), default=SourceTargetPolarity.INCLUDE)
    match_mode: Mapped[str] = mapped_column(String(20), default=SourceTargetMatchMode.EXACT)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    coverage: Mapped["SourceCoverage"] = relationship(back_populates="targets")

    __table_args__ = (
        UniqueConstraint(
            "source_coverage_id",
            "dimension",
            "target_key",
            name="uq_source_coverage_target_coverage_dimension_target",
        ),
        CheckConstraint("polarity IN ('include', 'exclude')", name="polarity"),
        CheckConstraint(
            "match_mode IN ('exact', 'hierarchical', 'covers', 'within')",
            name="match_mode",
        ),
        CheckConstraint("length(trim(dimension)) > 0", name="dimension_nonempty"),
        CheckConstraint("length(trim(target_key)) > 0", name="target_key_nonempty"),
        CheckConstraint("length(dimension) <= 100", name="dimension_length"),
        CheckConstraint("length(target_key) <= 300", name="target_key_length"),
        Index("ix_source_coverage_target_source_coverage_id", "source_coverage_id"),
        Index("ix_source_coverage_target_lookup", "dimension", "target_key", "polarity"),
    )


class SourceRelationship(Base):
    """Lineage, ownership, copying, or other independence-relevant source links."""

    __tablename__ = "source_relationship"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="CASCADE"), nullable=False
    )
    related_source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="CASCADE"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(50))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    origin: Mapped[str] = mapped_column(String(50), default="user")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    source_definition: Mapped["SourceDefinition"] = relationship(
        back_populates="outbound_relationships", foreign_keys=[source_definition_id]
    )
    related_source_definition: Mapped["SourceDefinition"] = relationship(
        back_populates="inbound_relationships", foreign_keys=[related_source_definition_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "source_definition_id",
            "related_source_definition_id",
            "relationship_type",
            name="uq_source_relationship_source_related_type",
        ),
        CheckConstraint(
            "source_definition_id <> related_source_definition_id", name="different_sources"
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
            name="validity_range",
        ),
        CheckConstraint("length(trim(relationship_type)) > 0", name="relationship_type_nonempty"),
        Index("ix_source_relationship_source_definition_id", "source_definition_id"),
        Index(
            "ix_source_relationship_related_source_definition_id",
            "related_source_definition_id",
        ),
        Index("ix_source_relationship_relationship_type", "relationship_type"),
    )


class SourceDocument(Base):
    """One retrieved artefact, immutable. Raw bytes live in the content-addressed store;
    repeat retrievals of identical content bump retrieval_count instead of duplicating."""

    __tablename__ = "source_document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="RESTRICT"), index=True
    )
    source_coverage_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("source_coverage.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(String(2000))
    canonical_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    retrieval_count: Mapped[int] = mapped_column(Integer, default=1)
    claimed_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    raw_storage_path: Mapped[str] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(100), nullable=True)
    access_policy_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    targeting_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    collector_version: Mapped[str] = mapped_column(String(50))
    parser_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Token-lean distilled text (distill.py): clean text extracted from raw content,
    # stored under data/processed/. The cheap input for AI/agents and later parsing.
    distilled_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    distilled_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)

    source_definition: Mapped["SourceDefinition"] = relationship(back_populates="documents")
    coverage: Mapped["SourceCoverage | None"] = relationship(
        back_populates="documents", foreign_keys=[source_coverage_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "source_definition_id",
            "source_url",
            "content_hash",
            name="uq_source_document_source_url_content_hash",
        ),
        ForeignKeyConstraint(
            ["source_coverage_id", "source_definition_id"],
            ["source_coverage.id", "source_coverage.source_definition_id"],
            name="fk_source_document_coverage_source",
            ondelete="RESTRICT",
        ),
        CheckConstraint("retrieval_count >= 1", name="retrieval_count_positive"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_nonnegative"),
        CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="http_status_range",
        ),
    )


class ResearchScope(Base):
    """Named, reusable industry/geography/facet research context.

    Exactly one scope is active at a time; it filters sources now and discovery,
    leads, and market outputs in later milestones.
    """

    __tablename__ = "research_scope"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    geo_codes: Mapped[list] = mapped_column(JSON, default=list)
    # Geographies to carve out of the scope. A source is dropped only when its entire
    # known footprint falls inside these codes (geography.excluded_by).
    exclude_codes: Mapped[list] = mapped_column(JSON, default=list)
    industry_ids: Mapped[list] = mapped_column(JSON, default=list)
    target_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    include_unknown: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        Index(
            "uq_research_scope_one_active",
            "is_active",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )


class GeoRegion(Base):
    """A named geography region as data (ADR-0012): user-editable membership.

    Rows are the source of truth for the in-process registry in
    heatseeker_core_domain.geography; builtins are seeded on first boot and stay
    editable, but cannot be deleted (scopes may reference them by habit and name).
    """

    __tablename__ = "geo_region"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    member_codes: Mapped[list] = mapped_column(JSON, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    __table_args__ = (
        CheckConstraint("length(trim(code)) >= 3 AND length(code) <= 50", name="code_length"),
        CheckConstraint("length(trim(name)) >= 1 AND length(name) <= 200", name="name_length"),
    )


class FrontierStatus(enum.StrEnum):
    QUEUED = "queued"
    FETCHED = "fetched"
    BLOCKED = "blocked"  # robots disallowed — recorded, never fetched (spec §35 M3)
    SKIPPED = "skipped"  # budget/expiry/vocabulary cut
    FAILED = "failed"


class CrawlFrontier(Base):
    """One queued crawl URL with full purpose + lineage (spec §11.6).

    Every row answers: why is this URL here (purpose, discovered_via, rule), where did
    it come from (parent_url, depth), and what happened (status, document_id).
    """

    __tablename__ = "crawl_frontier"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2000))
    normalised_url: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default=FrontierStatus.QUEUED, index=True)
    purpose: Mapped[str] = mapped_column(String(200), default="collection")
    discovered_via: Mapped[str] = mapped_column(String(30))  # seed|sitemap|link|manual
    discovery_rule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    parent_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    expected_content: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_document.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_crawl_frontier_source_url", "source_definition_id", "normalised_url", unique=True
        ),
        Index("ix_crawl_frontier_claim", "source_definition_id", "status", "priority"),
    )
