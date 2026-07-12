"""Entity core tables (spec §13.1-§13.5) and resolution records (spec §14).

Merges are pointers, never rewrites: an absorbed organisation keeps every child row and
gains `merged_into_id`; `entity_merge` holds the audit trail that makes reversal exact
(spec §14.4). See docs/milestones/M4-entities.md for the design rationale.
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


class OrganisationType(enum.StrEnum):
    COMPANY = "company"
    SOLE_TRADER = "sole_trader"
    PARTNERSHIP = "partnership"
    GROUP = "group"
    PUBLIC_AGENCY = "public_agency"
    ASSOCIATION = "association"
    OTHER = "other"
    UNKNOWN = "unknown"


class OrganisationStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEFUNCT = "defunct"
    MERGED = "merged"  # absorbed into another record — merged_into_id set, row preserved
    UNKNOWN = "unknown"


class EntityProvenance(enum.StrEnum):
    MANUAL = "manual"  # user input — distinguishable forever (spec §6.6, §12.4)
    INGESTION = "ingestion"  # created from evidence by a pipeline


class LocationType(enum.StrEnum):
    REGISTERED_ADDRESS = "registered_address"
    OFFICE = "office"
    YARD = "yard"
    DEPOT = "depot"
    PROJECT_SITE = "project_site"
    FACTORY = "factory"
    TRAINING_CENTRE = "training_centre"
    SERVICE_AREA_CENTROID = "service_area_centroid"
    MAILING_ADDRESS = "mailing_address"
    OTHER = "other"


class UnitType(enum.StrEnum):
    BRANCH = "branch"
    YARD = "yard"
    OFFICE = "office"
    DIVISION = "division"
    FRANCHISE = "franchise"
    BUSINESS_UNIT = "business_unit"
    OTHER = "other"


class UnitActiveStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class IdentifierScheme(enum.StrEnum):
    ABN = "abn"
    ACN = "acn"
    NZBN = "nzbn"
    LEI = "lei"
    COMPANY_NUMBER = "company_number"
    OTHER = "other"


class ContactType(enum.StrEnum):
    GENERAL_EMAIL = "general_email"
    ROLE_EMAIL = "role_email"
    PHONE = "phone"
    CONTACT_FORM = "contact_form"
    SOCIAL_PROFILE = "social_profile"
    POSTAL_ADDRESS = "postal_address"
    ENQUIRY_URL = "enquiry_url"
    OTHER = "other"


class MatchState(enum.StrEnum):
    """Spec §14.3 vocabulary."""

    EXACT = "exact"
    HIGH_CONFIDENCE_PROBABLE = "high_confidence_probable"
    POSSIBLE_REVIEW = "possible_review"
    RELATED_BUT_DISTINCT = "related_but_distinct"
    CONFIRMED_DISTINCT = "confirmed_distinct"
    UNRESOLVED = "unresolved"


class CandidateResolution(enum.StrEnum):
    MERGED = "merged"
    RELATED = "related"
    DISTINCT = "distinct"


class Location(Base):
    __tablename__ = "location"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    address_lines: Mapped[list] = mapped_column(JSON, default=list)
    locality: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    region: Mapped[str | None] = mapped_column(String(200), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_type: Mapped[str] = mapped_column(String(30), default=LocationType.OTHER)
    geocode_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class Organisation(Base):
    __tablename__ = "organisation"
    __table_args__ = (
        CheckConstraint("length(trim(canonical_name)) > 0", name="canonical_name_nonempty"),
        CheckConstraint("id != merged_into_id", name="no_self_merge"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    canonical_name: Mapped[str] = mapped_column(String(500), index=True)
    legal_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trading_names: Mapped[list] = mapped_column(JSON, default=list)
    organisation_type: Mapped[str] = mapped_column(String(30), default=OrganisationType.UNKNOWN)
    status: Mapped[str] = mapped_column(String(20), default=OrganisationStatus.ACTIVE, index=True)
    country_of_registration: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_organisation_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisation.id"), nullable=True
    )
    ultimate_parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisation.id"), nullable=True
    )
    primary_location_id: Mapped[str | None] = mapped_column(
        ForeignKey("location.id"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(20), default=EntityProvenance.MANUAL)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    profile_completeness: Mapped[float] = mapped_column(Float, default=0.0)
    entity_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    merged_into_id: Mapped[str | None] = mapped_column(
        ForeignKey("organisation.id"), nullable=True, index=True
    )

    primary_location: Mapped[Location | None] = relationship(
        foreign_keys=[primary_location_id], lazy="joined"
    )
    identifiers: Mapped[list["OrganisationIdentifier"]] = relationship(
        back_populates="organisation", order_by="OrganisationIdentifier.scheme"
    )
    domains: Mapped[list["OrganisationDomain"]] = relationship(
        back_populates="organisation", order_by="OrganisationDomain.domain"
    )
    contact_points: Mapped[list["ContactPoint"]] = relationship(
        back_populates="organisation", order_by="ContactPoint.contact_type"
    )
    units: Mapped[list["OperationalUnit"]] = relationship(
        back_populates="organisation", order_by="OperationalUnit.unit_type"
    )


class OperationalUnit(Base):
    __tablename__ = "operational_unit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    unit_type: Mapped[str] = mapped_column(String(30), default=UnitType.BRANCH)
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("location.id"), nullable=True)
    service_area: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active_status: Mapped[str] = mapped_column(String(20), default=UnitActiveStatus.UNKNOWN)

    organisation: Mapped[Organisation] = relationship(back_populates="units")
    location: Mapped[Location | None] = relationship(lazy="joined")


class OrganisationIdentifier(Base):
    __tablename__ = "organisation_identifier"
    __table_args__ = (
        UniqueConstraint("organisation_id", "scheme", "value_normalised"),
        Index("ix_organisation_identifier_lookup", "scheme", "value_normalised"),
        CheckConstraint("length(trim(value)) > 0", name="value_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    scheme: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(100))  # as observed
    value_normalised: Mapped[str] = mapped_column(String(100))  # matching key
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    organisation: Mapped[Organisation] = relationship(back_populates="identifiers")


class OrganisationDomain(Base):
    __tablename__ = "organisation_domain"
    __table_args__ = (
        UniqueConstraint("organisation_id", "domain"),
        CheckConstraint("length(trim(domain)) > 0", name="domain_nonempty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    domain: Mapped[str] = mapped_column(String(300), index=True)  # normalised host
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    organisation: Mapped[Organisation] = relationship(back_populates="domains")


class ContactPoint(Base):
    __tablename__ = "contact_point"
    __table_args__ = (CheckConstraint("length(trim(value)) > 0", name="value_nonempty"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    operational_unit_id: Mapped[str | None] = mapped_column(
        ForeignKey("operational_unit.id"), nullable=True
    )
    # person_id lands with Person/RoleAssignment (spec §20) — deliberately absent in M4.
    contact_type: Mapped[str] = mapped_column(String(30), default=ContactType.OTHER)
    value: Mapped[str] = mapped_column(String(500))
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    public_business_contact: Mapped[bool] = mapped_column(Boolean, default=True)
    role_based: Mapped[bool] = mapped_column(Boolean, default=False)
    first_observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deliverability_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_evidence_ids: Mapped[list] = mapped_column(JSON, default=list)

    organisation: Mapped[Organisation] = relationship(back_populates="contact_points")


class EntityMatchCandidate(Base):
    """A scored organisation pair awaiting (or holding) a human resolution decision."""

    __tablename__ = "entity_match_candidate"
    __table_args__ = (
        UniqueConstraint("organisation_a_id", "organisation_b_id"),
        # Canonical pair ordering: one row per unordered pair.
        CheckConstraint("organisation_a_id < organisation_b_id", name="pair_ordered"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    organisation_a_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    organisation_b_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    match_state: Mapped[str] = mapped_column(
        String(30), default=MatchState.UNRESOLVED, index=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0)
    signals: Mapped[list] = mapped_column(JSON, default=list)  # explainability (spec §14.2)
    conflict_count: Mapped[int] = mapped_column(default=0)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    organisation_a: Mapped[Organisation] = relationship(foreign_keys=[organisation_a_id])
    organisation_b: Mapped[Organisation] = relationship(foreign_keys=[organisation_b_id])


class EntityMerge(Base):
    """Audit record for one merge; carries everything needed to reverse it exactly."""

    __tablename__ = "entity_merge"
    __table_args__ = (CheckConstraint("survivor_id != absorbed_id", name="distinct_parties"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    survivor_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    absorbed_id: Mapped[str] = mapped_column(ForeignKey("organisation.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("entity_match_candidate.id"), nullable=True
    )
    rationale: Mapped[str] = mapped_column(Text)
    signals_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    absorbed_prior_status: Mapped[str] = mapped_column(String(20))
    performed_by: Mapped[str] = mapped_column(String(100), default="user")
    performed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reversed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    survivor: Mapped[Organisation] = relationship(foreign_keys=[survivor_id])
    absorbed: Mapped[Organisation] = relationship(foreign_keys=[absorbed_id])
