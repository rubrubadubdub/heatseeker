"""Size bands and operating tiers — evidence-backed or 'unresolved', never invented (§16).

Every estimate lists the indicators it used (with evidence references); when no
indicator exists the concept stays `unresolved`. Legal-entity, group, and branch sizes
are separate concepts and are never collapsed (§16.3, data-model distinction #3).
"""

from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.models import Organisation
from heatseeker_entity_resolution.resolution import merge_group
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence.capabilities import capabilities_for
from heatseeker_intelligence.models import (
    CapabilityStatus,
    FactAssertion,
    SizeConcept,
    SizeEstimate,
)
from heatseeker_intelligence.observations import PREDICATE_EMPLOYEES

SIZING_RULE_VERSION = "sizing/0.1"

# Recognised employee bands (input rows carry bands, never invented precision).
EMPLOYEE_BANDS = ("1-4", "5-19", "20-49", "50-199", "200+")


def _upsert(
    session: Session,
    organisation_id: str,
    concept: str,
    band: str,
    basis: list[dict],
    confidence: float,
) -> SizeEstimate:
    estimate = session.execute(
        select(SizeEstimate).where(
            SizeEstimate.organisation_id == organisation_id,
            SizeEstimate.concept == concept,
        )
    ).scalar_one_or_none()
    if estimate is None:
        estimate = SizeEstimate(organisation_id=organisation_id, concept=concept)
        session.add(estimate)
    elif (
        estimate.band == band
        and estimate.basis == basis
        and estimate.confidence == round(confidence, 3)
        and estimate.rule_version == SIZING_RULE_VERSION
    ):
        return estimate
    estimate.band = band
    estimate.basis = basis
    estimate.confidence = round(confidence, 3)
    estimate.rule_version = SIZING_RULE_VERSION
    estimate.estimated_at = utc_now()
    session.flush()
    return estimate


def _employee_fact(session: Session, entity_id: str) -> FactAssertion | None:
    return session.execute(
        select(FactAssertion).where(
            FactAssertion.subject_entity_id == entity_id,
            FactAssertion.predicate == PREDICATE_EMPLOYEES,
            FactAssertion.status.not_in(["disproven", "unknown"]),
        )
    ).scalar_one_or_none()


def estimate_sizes(session: Session, organisation_id: str) -> dict[str, SizeEstimate]:
    """Recompute all size concepts for one organisation (canonical merge-group view)."""
    group = merge_group(session, organisation_id)
    canonical = group[0]
    group_ids = [o.id for o in group]

    estimates: dict[str, SizeEstimate] = {}

    # Legal-entity size: employee band evidence or nothing.
    employee_fact = _employee_fact(session, canonical.id)
    if employee_fact is not None and employee_fact.value in EMPLOYEE_BANDS:
        estimates[SizeConcept.LEGAL_ENTITY_SIZE] = _upsert(
            session,
            canonical.id,
            SizeConcept.LEGAL_ENTITY_SIZE,
            str(employee_fact.value),
            [
                {
                    "indicator": "employee_count_band",
                    "fact_assertion_id": employee_fact.id,
                    "confidence": employee_fact.final_confidence,
                }
            ],
            employee_fact.final_confidence,
        )
    else:
        estimates[SizeConcept.LEGAL_ENTITY_SIZE] = _upsert(
            session, canonical.id, SizeConcept.LEGAL_ENTITY_SIZE, "unresolved", [], 0.0
        )

    # Operating-group size: group breadth = merge group + subsidiaries + units.
    subsidiaries = list(
        session.execute(
            select(Organisation).where(Organisation.parent_organisation_id.in_(group_ids))
        ).scalars()
    )
    unit_count = sum(len(o.units) for o in group)
    branch_basis: list[dict] = []
    if unit_count:
        branch_basis.append({"indicator": "operational_unit_count", "value": unit_count})
    if subsidiaries:
        branch_basis.append(
            {"indicator": "subsidiary_count", "value": len(subsidiaries)}
        )
    if branch_basis:
        breadth = unit_count + len(subsidiaries)
        band = "multi-site" if breadth >= 2 else "single-site"
        estimates[SizeConcept.OPERATING_GROUP_SIZE] = _upsert(
            session,
            canonical.id,
            SizeConcept.OPERATING_GROUP_SIZE,
            band,
            branch_basis,
            0.6,
        )
    else:
        estimates[SizeConcept.OPERATING_GROUP_SIZE] = _upsert(
            session, canonical.id, SizeConcept.OPERATING_GROUP_SIZE, "unresolved", [], 0.0
        )

    # Local-branch size: only when unit-level evidence exists (it usually doesn't yet).
    estimates[SizeConcept.LOCAL_BRANCH_SIZE] = _upsert(
        session, canonical.id, SizeConcept.LOCAL_BRANCH_SIZE, "unresolved", [], 0.0
    )

    # Capability tier (§16.4 example rubric, deterministic v1; pack thresholds later).
    capability_rows = capabilities_for(session, group_ids)
    evidenced = [
        c
        for c in capability_rows
        if c.capability_status
        in (
            CapabilityStatus.EVIDENCED,
            CapabilityStatus.REPEATEDLY_EVIDENCED,
            CapabilityStatus.VERIFIED,
        )
    ]
    claimed = [c for c in capability_rows if c.capability_status == CapabilityStatus.CLAIMED]
    tier_basis = [
        {
            "indicator": "capabilities_evidenced",
            "value": len(evidenced),
            "capability_ids": [c.capability_id for c in evidenced],
        },
        {"indicator": "capabilities_claimed", "value": len(claimed)},
        {"indicator": "operational_unit_count", "value": unit_count},
    ]
    employees_band = str(employee_fact.value) if employee_fact is not None else None
    if not capability_rows and not unit_count and employees_band is None:
        tier, tier_confidence = "unresolved", 0.0
        tier_basis = []
    elif (
        len(evidenced) >= 3
        and unit_count >= 2
        and employees_band in ("50-199", "200+")
    ):
        tier, tier_confidence = "A", 0.7
    elif len(evidenced) >= 2 and (unit_count >= 1 or employees_band in ("20-49", "50-199")):
        tier, tier_confidence = "B", 0.6
    elif evidenced:
        tier, tier_confidence = "C", 0.55
    else:
        tier, tier_confidence = "D", 0.4  # weak/claimed-only evidence (§16.4)
    estimates[SizeConcept.CAPABILITY_TIER] = _upsert(
        session, canonical.id, SizeConcept.CAPABILITY_TIER, tier, tier_basis, tier_confidence
    )

    # Sophistication/need concepts stay honest placeholders until their evidence
    # predicates exist (M8 lead intelligence) — unresolved, not guessed.
    for concept in (
        SizeConcept.COMMERCIAL_SOPHISTICATION,
        SizeConcept.PROCUREMENT_SOPHISTICATION,
        SizeConcept.OUTSOURCING_NEED,
    ):
        estimates[concept] = _upsert(session, canonical.id, concept, "unresolved", [], 0.0)

    return estimates


def estimates_for(session: Session, entity_ids: list[str]) -> list[SizeEstimate]:
    if not entity_ids:
        return []
    return list(
        session.execute(
            select(SizeEstimate)
            .where(SizeEstimate.organisation_id == entity_ids[0])
            .order_by(SizeEstimate.concept)
        ).scalars()
    )
