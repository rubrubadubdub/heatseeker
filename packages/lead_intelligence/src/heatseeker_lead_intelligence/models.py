"""Offering, lead, and suppression tables (spec §13.17, §19, §32.3).

An offering is what *we* sell; a lead only exists as (organisation x offering). Every
score dimension is stored with its reasons so the queue is explainable end to end, and
suppression is a first-class, reversible rule that all outputs respect.
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class OfferingStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class OpportunityStage(enum.StrEnum):
    IDENTIFIED = "identified"
    RESEARCHING = "researching"
    QUALIFIED = "qualified"
    SUPPRESSED = "suppressed"
    ARCHIVED = "archived"


class SuppressionReason(enum.StrEnum):
    OPT_OUT = "opt_out"
    DO_NOT_CONTACT = "do_not_contact"
    EXISTING_CLIENT = "existing_client"
    COMPETITOR = "competitor"
    OTHER = "other"


class Offering(Base):
    """A configured service we could sell (spec §19.1). User-defined, tunable."""

    __tablename__ = "offering"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OfferingStatus.ACTIVE, index=True)
    pack_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Fit configuration — pack-vocabulary ids, all optional.
    target_archetype_ids: Mapped[list] = mapped_column(JSON, default=list)
    target_capability_ids: Mapped[list] = mapped_column(JSON, default=list)
    # Capabilities whose *absence* suggests need for this offering (§19.3), and whose
    # evidenced presence is a negative indicator (§19.4 "mature internal team").
    need_gap_capability_ids: Mapped[list] = mapped_column(JSON, default=list)
    negative_archetype_ids: Mapped[list] = mapped_column(JSON, default=list)
    geo_codes: Mapped[list] = mapped_column(JSON, default=list)  # empty → active scope
    scoring_weights: Mapped[dict] = mapped_column(JSON, default=dict)  # overrides defaults
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class AccountOpportunity(Base):
    """One lead: organisation x offering, with inspectable scores (spec §13.17)."""

    __tablename__ = "account_opportunity"
    __table_args__ = (
        UniqueConstraint("organisation_id", "offering_id"),
        CheckConstraint(
            "commercial_priority >= 0 AND commercial_priority <= 1",
            name="priority_range",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    offering_id: Mapped[str] = mapped_column(ForeignKey("offering.id"), index=True)
    fit_score: Mapped[float] = mapped_column(Float, default=0.0)
    timing_score: Mapped[float] = mapped_column(Float, default=0.5)
    evidence_quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    accessibility_score: Mapped[float] = mapped_column(Float, default=0.0)
    relationship_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    commercial_priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    opportunity_stage: Mapped[str] = mapped_column(
        String(20), default=OpportunityStage.IDENTIFIED, index=True
    )
    component_scores: Mapped[dict] = mapped_column(JSON, default=dict)  # §19.2 detail
    reasons: Mapped[list] = mapped_column(JSON, default=list)  # [{dimension,text,evidence}]
    risks: Mapped[list] = mapped_column(JSON, default=list)
    unknowns: Mapped[list] = mapped_column(JSON, default=list)
    next_action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(50), default="")
    scored_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    organisation = relationship("Organisation", lazy="joined")
    offering: Mapped[Offering] = relationship(lazy="joined")


class SuppressionRule(Base):
    """Do-not-approach rule for one organisation (spec §32.3). Reversible, audited."""

    __tablename__ = "suppression_rule"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    reason: Mapped[str] = mapped_column(String(30), default=SuppressionReason.OTHER)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="user")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    lifted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lifted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    organisation = relationship("Organisation", lazy="joined")
