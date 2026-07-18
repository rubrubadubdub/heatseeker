"""Offering management, lead rescoring, and suppression (spec §19, §32.3)."""

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.models import Organisation, OrganisationStatus
from heatseeker_entity_resolution.resolution import canonical_id
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_lead_intelligence.models import (
    AccountOpportunity,
    Offering,
    OfferingStatus,
    OpportunityStage,
    SuppressionRule,
)
from heatseeker_lead_intelligence.scoring import SCORING_RULE_VERSION, score_organisation


def create_offering(
    session: Session,
    name: str,
    *,
    description: str | None = None,
    pack_id: str | None = None,
    target_archetype_ids: list[str] | None = None,
    target_capability_ids: list[str] | None = None,
    need_gap_capability_ids: list[str] | None = None,
    negative_archetype_ids: list[str] | None = None,
    geo_codes: list[str] | None = None,
    scoring_weights: dict | None = None,
    actor: str = "user",
) -> Offering:
    name = name.strip()
    if not name:
        raise ValueError("offering name must not be blank")
    offering = Offering(
        name=name,
        description=(description or "").strip() or None,
        pack_id=pack_id,
        target_archetype_ids=target_archetype_ids or [],
        target_capability_ids=target_capability_ids or [],
        need_gap_capability_ids=need_gap_capability_ids or [],
        negative_archetype_ids=negative_archetype_ids or [],
        geo_codes=geo_codes or [],
        scoring_weights=scoring_weights or {},
    )
    session.add(offering)
    session.flush()
    audit.record(session, actor, "offering.created", "offering", offering.id, {"name": name})
    return offering


def list_offerings(session: Session, *, include_archived: bool = False) -> list[Offering]:
    stmt = select(Offering).order_by(Offering.name)
    if not include_archived:
        stmt = stmt.where(Offering.status == OfferingStatus.ACTIVE)
    return list(session.execute(stmt).scalars())


def active_suppressions(
    session: Session, organisation_ids: list[str]
) -> dict[str, SuppressionRule]:
    rules = session.execute(
        select(SuppressionRule).where(
            SuppressionRule.organisation_id.in_(organisation_ids),
            SuppressionRule.active.is_(True),
        )
    ).scalars()
    return {rule.organisation_id: rule for rule in rules}


def suppress(
    session: Session,
    organisation_id: str,
    *,
    reason: str,
    note: str | None = None,
    actor: str = "user",
) -> SuppressionRule:
    organisation_id = canonical_id(session, organisation_id)
    existing = active_suppressions(session, [organisation_id]).get(organisation_id)
    if existing is not None:
        return existing
    rule = SuppressionRule(
        organisation_id=organisation_id, reason=reason, note=note, created_by=actor
    )
    session.add(rule)
    # Any existing leads collapse to suppressed immediately.
    for lead in session.execute(
        select(AccountOpportunity).where(
            AccountOpportunity.organisation_id == organisation_id
        )
    ).scalars():
        lead.opportunity_stage = OpportunityStage.SUPPRESSED
        lead.commercial_priority = 0.0
    session.flush()
    audit.record(
        session, actor, "lead.suppressed", "organisation", organisation_id, {"reason": reason}
    )
    return rule


def lift_suppression(
    session: Session, rule_id: str, *, actor: str = "user"
) -> SuppressionRule:
    rule = session.get(SuppressionRule, rule_id)
    if rule is None:
        raise LookupError(f"suppression rule not found: {rule_id}")
    rule.active = False
    rule.lifted_at = utc_now()
    rule.lifted_by = actor
    session.flush()
    audit.record(
        session, actor, "lead.suppression_lifted", "organisation", rule.organisation_id, {}
    )
    return rule


def rescore_offering(
    session: Session, offering_id: str, *, actor: str = "system", max_organisations: int = 5000
) -> dict:
    """Recompute every lead for one offering. Deterministic; suppression wins."""
    offering = session.get(Offering, offering_id)
    if offering is None:
        raise LookupError(f"offering not found: {offering_id}")

    organisations = list(
        session.execute(
            select(Organisation)
            .where(
                Organisation.status.not_in(
                    [OrganisationStatus.MERGED, OrganisationStatus.DEFUNCT]
                )
            )
            .limit(max_organisations)
        ).scalars()
    )
    suppressed = active_suppressions(session, [o.id for o in organisations])
    existing = {
        lead.organisation_id: lead
        for lead in session.execute(
            select(AccountOpportunity).where(AccountOpportunity.offering_id == offering_id)
        ).scalars()
    }

    counts = {"scored": 0, "suppressed": 0, "skipped": 0}
    for organisation in organisations:
        score = score_organisation(session, organisation, offering)
        if score.skip:
            counts["skipped"] += 1
            continue
        lead = existing.get(organisation.id)
        if lead is None:
            lead = AccountOpportunity(
                organisation_id=organisation.id, offering_id=offering_id
            )
            session.add(lead)
        rule = suppressed.get(organisation.id)
        lead.fit_score = score.fit
        lead.timing_score = score.timing
        lead.evidence_quality_score = score.evidence_quality
        lead.accessibility_score = score.accessibility
        lead.relationship_score = score.relationship
        lead.component_scores = score.components
        lead.reasons = score.reasons
        lead.risks = score.risks + (
            [f"suppressed: {rule.reason}" + (f" — {rule.note}" if rule.note else "")]
            if rule
            else []
        )
        lead.unknowns = score.unknowns
        lead.next_action = score.next_action
        lead.rule_version = SCORING_RULE_VERSION
        lead.scored_at = utc_now()
        if rule is not None:
            lead.opportunity_stage = OpportunityStage.SUPPRESSED
            lead.commercial_priority = 0.0
            counts["suppressed"] += 1
        else:
            if lead.opportunity_stage == OpportunityStage.SUPPRESSED:
                lead.opportunity_stage = OpportunityStage.IDENTIFIED
            lead.commercial_priority = score.commercial_priority
            counts["scored"] += 1
    session.flush()
    audit.record(session, actor, "leads.rescored", "offering", offering_id, counts)
    return {"offering": offering.name, **counts}


def lead_queue(
    session: Session,
    offering_id: str,
    *,
    include_suppressed: bool = False,
    limit: int = 500,
) -> list[AccountOpportunity]:
    stmt = (
        select(AccountOpportunity)
        .where(AccountOpportunity.offering_id == offering_id)
        .order_by(AccountOpportunity.commercial_priority.desc())
        .limit(limit)
    )
    if not include_suppressed:
        stmt = stmt.where(
            AccountOpportunity.opportunity_stage != OpportunityStage.SUPPRESSED
        )
    return list(session.execute(stmt).scalars())


def leads_for_organisation(
    session: Session, organisation_id: str
) -> list[AccountOpportunity]:
    return list(
        session.execute(
            select(AccountOpportunity)
            .where(AccountOpportunity.organisation_id == organisation_id)
            .order_by(AccountOpportunity.commercial_priority.desc())
        ).scalars()
    )
