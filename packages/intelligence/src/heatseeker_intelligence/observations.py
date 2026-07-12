"""Recording and querying observations — what a source said, never overwritten."""

import json
from datetime import datetime

from heatseeker_common.timeutil import utc_now
from heatseeker_source_registry.models import SourceDocument
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence.models import (
    ExtractionMethod,
    NormalisationStatus,
    Observation,
)

# Core predicates used by discovery and profile assembly. Free-form predicates are
# allowed — packs and later milestones add their own — these are just the shared names.
PREDICATE_CANONICAL_NAME = "canonical_name"
PREDICATE_LEGAL_NAME = "legal_name"
PREDICATE_TRADING_NAME = "trading_name"
PREDICATE_IDENTIFIER = "registration_identifier"
PREDICATE_REGISTRATION_STATUS = "registration_status"
PREDICATE_DOMAIN = "website_domain"
PREDICATE_PHONE = "phone"
PREDICATE_EMAIL = "email"
PREDICATE_LOCATION = "location"
PREDICATE_EMPLOYEES = "employee_count_band"
PREDICATE_DESCRIPTION = "description"
PREDICATE_SERVICE_CLAIM = "service_claim"
PREDICATE_ARCHETYPE_CLAIM = "archetype_claim"


def record_observation(
    session: Session,
    document: SourceDocument,
    predicate: str,
    object_value,
    *,
    subject_entity_id: str | None = None,
    extraction_method: str = ExtractionMethod.DETERMINISTIC,
    extraction_confidence: float = 0.8,
    source_location: dict | None = None,
    observed_at: datetime | None = None,
    normalisation_status: str = NormalisationStatus.NORMALISED,
    human_verified: bool = False,
    verified_by: str | None = None,
) -> Observation:
    if not predicate.strip():
        raise ValueError("observation predicate must not be blank")
    observation = Observation(
        subject_entity_id=subject_entity_id,
        predicate=predicate.strip(),
        object_value=object_value,
        source_document_id=document.id,
        source_location=source_location,
        extraction_method=extraction_method,
        extraction_confidence=max(0.0, min(1.0, extraction_confidence)),
        normalisation_status=normalisation_status,
        human_verified=human_verified,
        verified_by=verified_by if human_verified else None,
        verified_at=utc_now() if human_verified else None,
    )
    if observed_at is not None:
        observation.observed_at = observed_at
    session.add(observation)
    session.flush()
    return observation


def observations_for(
    session: Session, subject_entity_ids: list[str], predicate: str | None = None
) -> list[Observation]:
    stmt = select(Observation).where(Observation.subject_entity_id.in_(subject_entity_ids))
    if predicate is not None:
        stmt = stmt.where(Observation.predicate == predicate)
    return list(session.execute(stmt.order_by(Observation.observed_at)).scalars())


def value_key(value) -> str:
    """Canonical comparison key so equal values group together during reconciliation."""
    if isinstance(value, str):
        return value.strip().casefold()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).casefold()
