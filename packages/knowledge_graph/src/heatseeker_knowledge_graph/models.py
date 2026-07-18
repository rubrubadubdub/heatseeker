"""Project, participation, and relationship tables (spec §13.9-§13.12, §23.2).

Edges are typed, directed, confidence-scored, time-bound, evidence-backed, and never
deleted: ending or retracting an edge stamps dates and status so history survives.
Type vocabularies are extensible strings — the spec's examples ship as constants,
industry packs may add more without core changes.
"""

import enum
import uuid
from datetime import datetime

from heatseeker_common.db import Base, UTCDateTime
from heatseeker_common.timeutil import utc_now
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class ProjectStatus(enum.StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ParticipationStatus(enum.StrEnum):
    UNCONFIRMED = "unconfirmed"
    PROBABLE = "probable"
    CONFIRMED = "confirmed"
    HISTORICAL = "historical"
    RETRACTED = "retracted"


class RelationshipStatus(enum.StrEnum):
    ACTIVE = "active"
    HISTORICAL = "historical"  # ended — dates retained
    RETRACTED = "retracted"  # judged wrong — row retained for audit


# Spec §13.10 suggested participation roles (extensible).
PARTICIPATION_ROLES = (
    "asset_owner",
    "developer",
    "principal_contractor",
    "engineering_consultant",
    "scaffold_contractor",
    "temporary_works_designer",
    "equipment_supplier",
    "subcontractor",
    "bidder",
    "award_recipient",
    "unconfirmed_participant",
)

# Spec §13.12 suggested relationship types (extensible).
RELATIONSHIP_TYPES = (
    "parent_of",
    "subsidiary_of",
    "trading_name_of",
    "branch_of",
    "supplier_to",
    "customer_of",
    "contractor_on",
    "competitor_of",
    "distributor_of",
    "uses_system",
    "member_of_association",
    "certified_by",
    "serves_geography",
    "employs_role",
    "acquired_by",
    "partner_of",
    "likely_adjacent_provider",
)


class Project(Base):
    """Spec §13.9."""

    __tablename__ = "project"
    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        CheckConstraint(
            "estimated_value IS NULL OR estimated_value >= 0", name="value_nonnegative"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(500), index=True)
    project_type_ids: Mapped[list] = mapped_column(JSON, default=list)  # pack vocabulary
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.UNKNOWN, index=True)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("location.id"), nullable=True)
    geography_scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    estimated_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expected_start_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    expected_end_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sector_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    location = relationship("Location", lazy="joined")
    participations: Mapped[list["ProjectParticipation"]] = relationship(
        back_populates="project", order_by="ProjectParticipation.role_type"
    )


class ProjectParticipation(Base):
    """Spec §13.10 — an organisation's typed, confidence-scored role on a project."""

    __tablename__ = "project_participation"
    __table_args__ = (
        UniqueConstraint("project_id", "organisation_id", "role_type"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint(
            "contract_value IS NULL OR contract_value >= 0", name="value_nonnegative"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id"), index=True)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    role_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default=ParticipationStatus.UNCONFIRMED)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    contract_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    project: Mapped[Project] = relationship(back_populates="participations")
    organisation = relationship("Organisation", lazy="joined")


class Relationship(Base):
    """Spec §13.12 — typed, directed, time-bound, evidence-backed edge."""

    __tablename__ = "relationship"
    __table_args__ = (
        CheckConstraint("subject_entity_id != object_entity_id", name="no_self_edge"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    subject_entity_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    object_entity_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(
        String(20), default=RelationshipStatus.ACTIVE, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    subject = relationship("Organisation", foreign_keys=[subject_entity_id])
    object = relationship("Organisation", foreign_keys=[object_entity_id])
