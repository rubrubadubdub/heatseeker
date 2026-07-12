"""Evidence-chain and profile-intelligence tables (spec §13.6-§13.7, §13.14-§13.16, §16.3).

The chain SourceDocument → Observation → FactAssertion is mandatory (§6.1): an
observation records what one source said (immutable once written, contradictions
included); a fact assertion records what we currently conclude, with every confidence
component stored so the conclusion stays inspectable (§17.2).
"""

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
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class ExtractionMethod(enum.StrEnum):
    MANUAL = "manual"  # user-entered — distinguishable forever (§6.6)
    DETERMINISTIC = "deterministic"  # code parser/rule
    IMPORT = "import"  # bulk dataset row (§12.2)
    AI = "ai"  # schema-constrained AI extraction (M11 seam)


class NormalisationStatus(enum.StrEnum):
    RAW = "raw"
    NORMALISED = "normalised"
    REJECTED = "rejected"


class FactStatus(enum.StrEnum):
    """Spec §13.15 vocabulary."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    POSSIBLE = "possible"
    CONFLICTED = "conflicted"
    STALE = "stale"
    DISPROVEN = "disproven"
    UNKNOWN = "unknown"


class AssignmentType(enum.StrEnum):
    """Spec §13.6 vocabulary."""

    REGISTERED = "registered"
    SELF_DESCRIBED = "self_described"
    OBSERVED = "observed"
    INFERRED = "inferred"
    HUMAN_CONFIRMED = "human_confirmed"
    REJECTED = "rejected"


class CapabilityStatus(enum.StrEnum):
    """Spec §13.7 ladder."""

    CLAIMED = "claimed"
    EVIDENCED = "evidenced"
    REPEATEDLY_EVIDENCED = "repeatedly_evidenced"
    VERIFIED = "verified"
    HISTORICAL = "historical"
    UNCERTAIN = "uncertain"
    CONTRADICTED = "contradicted"


class SizeConcept(enum.StrEnum):
    """Spec §16.3 — distinct estimates, never collapsed."""

    LEGAL_ENTITY_SIZE = "legal_entity_size"
    OPERATING_GROUP_SIZE = "operating_group_size"
    LOCAL_BRANCH_SIZE = "local_branch_size"
    CAPABILITY_TIER = "capability_tier"
    COMMERCIAL_SOPHISTICATION = "commercial_sophistication"
    PROCUREMENT_SOPHISTICATION = "procurement_sophistication"
    OUTSOURCING_NEED = "outsourcing_need"


class QuestionStatus(enum.StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ImportRunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Observation(Base):
    """Atomic extracted statement tied to evidence (spec §13.14)."""

    __tablename__ = "observation"
    __table_args__ = (
        Index("ix_observation_subject_predicate", "subject_entity_id", "predicate"),
        CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="extraction_confidence_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    subject_entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisation.id"), nullable=True, index=True
    )
    predicate: Mapped[str] = mapped_column(String(100), index=True)
    object_value: Mapped[dict | list | str | int | float | None] = mapped_column(
        JSON, nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    source_document_id: Mapped[str] = mapped_column(
        ForeignKey("source_document.id"), index=True
    )
    source_location: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # row/page/xpath
    extraction_method: Mapped[str] = mapped_column(
        String(20), default=ExtractionMethod.DETERMINISTIC
    )
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.8)
    normalisation_status: Mapped[str] = mapped_column(
        String(20), default=NormalisationStatus.NORMALISED
    )
    human_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class FactAssertion(Base):
    """Reconciled conclusion per (entity, predicate) with inspectable components (§13.15)."""

    __tablename__ = "fact_assertion"
    __table_args__ = (
        UniqueConstraint("subject_entity_id", "predicate"),
        CheckConstraint(
            "final_confidence >= 0 AND final_confidence <= 1", name="final_confidence_range"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    subject_entity_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    predicate: Mapped[str] = mapped_column(String(100), index=True)
    value: Mapped[dict | list | str | int | float | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=FactStatus.UNKNOWN, index=True)
    authority_score: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_score: Mapped[float] = mapped_column(Float, default=0.0)
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0)
    corroboration_score: Mapped[float] = mapped_column(Float, default=0.0)
    contradiction_score: Mapped[float] = mapped_column(Float, default=0.0)
    final_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_vocabulary: Mapped[str] = mapped_column(String(20), default="unknown")
    supporting_observation_ids: Mapped[list] = mapped_column(JSON, default=list)
    contradicting_observation_ids: Mapped[list] = mapped_column(JSON, default=list)
    independent_source_count: Mapped[int] = mapped_column(default=0)
    best_evidence_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_document.id"), nullable=True
    )
    last_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ClassificationAssignment(Base):
    """Entity x pack taxonomy category with explainability (spec §13.6, §15.3).

    Taxonomies are pack data; pack_id/taxonomy_id/category_id are pack-scoped strings so
    a second industry never needs core-table changes (§41.18).
    """

    __tablename__ = "classification_assignment"
    __table_args__ = (
        UniqueConstraint("entity_id", "pack_id", "taxonomy_id", "category_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    entity_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    pack_id: Mapped[str] = mapped_column(String(100))  # "" for the fixed spec axes
    taxonomy_id: Mapped[str] = mapped_column(String(100))  # e.g. company_archetypes
    category_id: Mapped[str] = mapped_column(String(200))
    category_label: Mapped[str] = mapped_column(String(300), default="")
    assignment_type: Mapped[str] = mapped_column(String(20), default=AssignmentType.OBSERVED)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|retracted
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)  # observation ids
    classifier_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class CapabilityAssignment(Base):
    """Organisation x pack service capability with status ladder (spec §13.7)."""

    __tablename__ = "capability_assignment"
    __table_args__ = (
        UniqueConstraint("organisation_id", "pack_id", "capability_id"),
        CheckConstraint(
            "evidence_strength >= 0 AND evidence_strength <= 1", name="evidence_strength_range"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    pack_id: Mapped[str] = mapped_column(String(100))
    capability_id: Mapped[str] = mapped_column(String(200))
    capability_label: Mapped[str] = mapped_column(String(300), default="")
    capability_status: Mapped[str] = mapped_column(
        String(30), default=CapabilityStatus.UNCERTAIN, index=True
    )
    evidence_strength: Mapped[float] = mapped_column(Float, default=0.0)
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)
    geographic_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    scale_indicator: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class SizeEstimate(Base):
    """Band/tier per organisation x size concept — never fabricated precision (§16)."""

    __tablename__ = "size_estimate"
    __table_args__ = (UniqueConstraint("organisation_id", "concept"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    concept: Mapped[str] = mapped_column(String(40))
    band: Mapped[str] = mapped_column(String(100), default="unresolved")
    basis: Mapped[list] = mapped_column(JSON, default=list)  # indicators + evidence ids
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rule_version: Mapped[str] = mapped_column(String(50), default="")
    estimated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class ResearchQuestion(Base):
    """Generated or manual gap/contradiction to investigate (spec §13.16, §18.7)."""

    __tablename__ = "research_question"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    entity_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisation.id"), nullable=True, index=True
    )
    question_type: Mapped[str] = mapped_column(String(100), index=True)
    question_text: Mapped[str] = mapped_column(Text)
    priority: Mapped[float] = mapped_column(Float, default=0.5)
    generated_by: Mapped[str] = mapped_column(String(20), default="system")  # system|user|ai
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=QuestionStatus.OPEN, index=True)
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    resolution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class BulkImportRun(Base):
    """Full provenance for one bulk dataset import (spec §12.2)."""

    __tablename__ = "bulk_import_run"
    __table_args__ = (
        CheckConstraint(
            "authority_tier >= 1 AND authority_tier <= 7",
            name="authority_tier_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    dataset_name: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str | None] = mapped_column(String(300), nullable=True)
    dataset_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    coverage_date: Mapped[str | None] = mapped_column(String(50), nullable=True)
    licence_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    scope_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pack_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    authority_tier: Mapped[int] = mapped_column(default=5)
    source_definition_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_definition.id"), nullable=True
    )
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_document.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ImportRunStatus.QUEUED, index=True)
    row_count: Mapped[int] = mapped_column(default=0)
    imported_count: Mapped[int] = mapped_column(default=0)
    matched_existing_count: Mapped[int] = mapped_column(default=0)
    skipped_out_of_scope_count: Mapped[int] = mapped_column(default=0)
    rejected_count: Mapped[int] = mapped_column(default=0)
    rejected_samples: Mapped[list] = mapped_column(JSON, default=list)
    transformation_version: Mapped[str] = mapped_column(String(50), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    source_document = relationship("SourceDocument", foreign_keys=[source_document_id])
