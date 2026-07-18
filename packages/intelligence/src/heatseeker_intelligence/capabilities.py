"""Capability status ladder driven by evidence shape (spec §13.7).

claimed → evidenced → repeatedly-evidenced → verified is earned, not asserted:
self-description alone can never pass "claimed" (§39.2 marketing ≠ capability), and a
conflicting observation parks the capability at "contradicted" until a human resolves it.
"""

from datetime import datetime

from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence import confidence as conf
from heatseeker_intelligence.models import CapabilityAssignment, CapabilityStatus

CAPABILITY_RULE_VERSION = "capability/0.1"

_SELF_CATEGORIES = {"company_website"}
_HYPOTHESIS_ONLY_CATEGORIES = {"directory", "job_board", "news", "weak_signal"}
HISTORICAL_AFTER_DAYS = 3 * 365.0


def record_capability_evidence(
    session: Session,
    organisation_id: str,
    *,
    pack_id: str,
    capability_id: str,
    capability_label: str = "",
    observation_id: str,
    source_definition_id: str,
    source_category: str | None,
    authority_tier: int = 3,
    observed_at: datetime,
    contradicts: bool = False,
    geographic_scope: dict | None = None,
    scale_indicator: dict | None = None,
) -> CapabilityAssignment:
    """Attach one piece of capability evidence and recompute the ladder status."""
    assignment = session.execute(
        select(CapabilityAssignment).where(
            CapabilityAssignment.organisation_id == organisation_id,
            CapabilityAssignment.pack_id == pack_id,
            CapabilityAssignment.capability_id == capability_id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        assignment = CapabilityAssignment(
            organisation_id=organisation_id,
            pack_id=pack_id,
            capability_id=capability_id,
            capability_label=capability_label,
        )
        session.add(assignment)
        session.flush()

    entry = {
        "observation_id": observation_id,
        "source_definition_id": source_definition_id,
        "source_category": source_category,
        "observed_at": observed_at.isoformat(),
        "contradicts": contradicts,
        "self_described": (source_category or "") in _SELF_CATEGORIES,
        "hypothesis_only": (
            (source_category or "") in _HYPOTHESIS_ONLY_CATEGORIES
            or authority_tier >= 5
        ),
    }
    ledger = [e for e in assignment.evidence_ids if e.get("observation_id") != observation_id]
    ledger.append(entry)
    assignment.evidence_ids = ledger
    if geographic_scope:
        assignment.geographic_scope = geographic_scope
    if scale_indicator:
        assignment.scale_indicator = scale_indicator
    refresh_status(assignment)
    session.flush()
    return assignment


def verify_capability(
    session: Session, assignment_id: str, *, actor: str = "user"
) -> CapabilityAssignment:
    """Human verification — the only path to 'verified' (§13.7)."""
    assignment = session.get(CapabilityAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"capability assignment not found: {assignment_id}")
    assignment.capability_status = CapabilityStatus.VERIFIED
    assignment.evidence_strength = 1.0
    assignment.updated_at = utc_now()
    session.flush()
    return assignment


def refresh_status(assignment: CapabilityAssignment, now: datetime | None = None) -> str:
    """Recompute ladder status from the evidence ledger. Verified is sticky (human)."""
    if assignment.capability_status == CapabilityStatus.VERIFIED:
        return assignment.capability_status
    now = now or utc_now()
    ledger = assignment.evidence_ids or []
    supporting = [e for e in ledger if not e.get("contradicts")]
    contradicting = [e for e in ledger if e.get("contradicts")]
    independent = {
        e["source_definition_id"]
        for e in supporting
        if not e.get("self_described") and not e.get("hypothesis_only")
    }
    newest = None
    for entry in supporting:
        observed = datetime.fromisoformat(entry["observed_at"])
        if newest is None or observed > newest:
            newest = observed

    if contradicting:
        status = CapabilityStatus.CONTRADICTED
    elif not supporting:
        status = CapabilityStatus.UNCERTAIN
    elif newest is not None and (now - newest).total_seconds() / 86400.0 > HISTORICAL_AFTER_DAYS:
        status = CapabilityStatus.HISTORICAL
    elif len(supporting) >= 3 and len(independent) >= 2:
        status = CapabilityStatus.REPEATEDLY_EVIDENCED
    elif independent:
        status = CapabilityStatus.EVIDENCED
    elif supporting and all(e.get("hypothesis_only") for e in supporting):
        status = CapabilityStatus.UNCERTAIN
    else:
        status = CapabilityStatus.CLAIMED  # self-description only (§39.2)

    strength_map = {
        CapabilityStatus.REPEATEDLY_EVIDENCED: 0.85,
        CapabilityStatus.EVIDENCED: 0.65,
        CapabilityStatus.CLAIMED: 0.35,
        CapabilityStatus.HISTORICAL: 0.3,
        CapabilityStatus.UNCERTAIN: 0.1,
        CapabilityStatus.CONTRADICTED: 0.2,
    }
    assignment.capability_status = status
    assignment.evidence_strength = strength_map[status]
    assignment.recency_score = (
        conf.freshness_score("service_claim", newest, now) if newest is not None else 0.0
    )
    assignment.updated_at = utc_now()
    return status


def capabilities_for(session: Session, entity_ids: list[str]) -> list[CapabilityAssignment]:
    return list(
        session.execute(
            select(CapabilityAssignment)
            .where(CapabilityAssignment.organisation_id.in_(entity_ids))
            .order_by(CapabilityAssignment.capability_id)
        ).scalars()
    )
