"""Deterministic, explained lead scoring (spec §19).

Every dimension is computed by code from existing evidence-backed records, emits
reasons with evidence references, and records what it *doesn't* know as unknowns —
absence of evidence produces hypotheses and uncertainty, never fabricated scores.
"""

from dataclasses import dataclass, field

from heatseeker_core_domain.geography import GeographyMatchMode, match_geography
from heatseeker_entity_resolution.models import ContactType, Organisation
from heatseeker_entity_resolution.resolution import merge_group
from heatseeker_intelligence.capabilities import capabilities_for
from heatseeker_intelligence.classifications import classifications_for
from heatseeker_intelligence.facts import assertions_for
from heatseeker_intelligence.models import CapabilityStatus, SizeConcept
from heatseeker_intelligence.sizing import estimates_for
from heatseeker_knowledge_graph.graph import edges_for
from sqlalchemy.orm import Session

from heatseeker_lead_intelligence.models import Offering

SCORING_RULE_VERSION = "leads/0.1"

DEFAULT_WEIGHTS = {
    "industry_fit": 0.30,
    "service_fit": 0.25,
    "need_likelihood": 0.20,
    "scale": 0.15,
    "geographic_fit": 0.10,
}
# commercial_priority = fit·0.55 + timing·0.15 + evidence·0.15 + accessibility·0.15,
# then scaled by evidence quality so thin evidence always ranks lower (§35 M8 acc. #3).
PRIORITY_MIX = {"fit": 0.55, "timing": 0.15, "evidence": 0.15, "accessibility": 0.15}
TIMING_STUB = 0.5  # neutral until M7 lands (ADR-0015)

_CAPABILITY_STRENGTH = {
    CapabilityStatus.VERIFIED: 1.0,
    CapabilityStatus.REPEATEDLY_EVIDENCED: 0.85,
    CapabilityStatus.EVIDENCED: 0.7,
    CapabilityStatus.CLAIMED: 0.4,
    CapabilityStatus.HISTORICAL: 0.25,
    CapabilityStatus.UNCERTAIN: 0.1,
    CapabilityStatus.CONTRADICTED: 0.1,
}

_TIER_SCORES = {"A": 0.9, "B": 0.8, "C": 0.6, "D": 0.35, "unresolved": 0.4}

# §20.2 contact-route priority, best route wins.
_CONTACT_ROUTE_SCORES = (
    (ContactType.ROLE_EMAIL, 1.0, "public role-based email"),
    (ContactType.GENERAL_EMAIL, 0.85, "public general business email"),
    (ContactType.CONTACT_FORM, 0.7, "official contact form"),
    (ContactType.ENQUIRY_URL, 0.7, "public enquiry route"),
    (ContactType.PHONE, 0.6, "public phone"),
    (ContactType.SOCIAL_PROFILE, 0.5, "public business profile"),
    (ContactType.POSTAL_ADDRESS, 0.3, "postal address only"),
)


@dataclass
class LeadScore:
    fit: float = 0.0
    timing: float = TIMING_STUB
    evidence_quality: float = 0.0
    accessibility: float = 0.0
    relationship: float | None = None
    commercial_priority: float = 0.0
    components: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    next_action: dict | None = None
    skip: bool = False  # organisation should not be scored at all (§19.4 hard negatives)


def _reason(dimension: str, text: str, evidence: list | None = None) -> dict:
    return {"dimension": dimension, "text": text, "evidence": evidence or []}


def score_organisation(
    session: Session, organisation: Organisation, offering: Offering
) -> LeadScore:
    result = LeadScore()
    weights = {**DEFAULT_WEIGHTS, **(offering.scoring_weights or {})}

    if organisation.status in ("inactive", "defunct", "merged"):
        result.skip = True
        return result

    group = merge_group(session, organisation.id)
    group_ids = [o.id for o in group]

    classifications = classifications_for(session, group_ids)
    capabilities = capabilities_for(session, group_ids)
    capability_by_id = {c.capability_id: c for c in capabilities}
    size_by_concept = {e.concept: e for e in estimates_for(session, group_ids)}
    assertions = assertions_for(session, organisation.id)

    # --- industry fit -------------------------------------------------------
    industry_fit = 0.0
    if offering.target_archetype_ids:
        matches = [
            c
            for c in classifications
            if c.category_id in offering.target_archetype_ids
            and c.assignment_type != "rejected"
        ]
        if matches:
            best = max(matches, key=lambda c: c.confidence)
            industry_fit = round(0.5 + 0.5 * best.confidence, 3)
            result.reasons.append(
                _reason(
                    "industry_fit",
                    f"classified as {best.category_label or best.category_id} "
                    f"({best.assignment_type}, confidence {best.confidence:.2f})",
                    [f"classification:{m.id}" for m in matches],
                )
            )
        else:
            result.unknowns.append(
                "no classification matches the offering's target archetypes"
            )
    elif classifications:
        industry_fit = 0.5
        result.reasons.append(
            _reason("industry_fit", "offering targets no archetypes; classified company")
        )
    else:
        result.unknowns.append("company has no classifications yet")

    # --- service fit --------------------------------------------------------
    service_fit = 0.0
    if offering.target_capability_ids:
        hits = [
            capability_by_id[cap_id]
            for cap_id in offering.target_capability_ids
            if cap_id in capability_by_id
        ]
        if hits:
            service_fit = round(
                max(_CAPABILITY_STRENGTH.get(h.capability_status, 0.1) for h in hits), 3
            )
            for hit in hits:
                result.reasons.append(
                    _reason(
                        "service_fit",
                        f"{hit.capability_label or hit.capability_id} is "
                        f"{hit.capability_status} ({len(hit.evidence_ids)} evidence)",
                        [f"capability:{hit.id}"],
                    )
                )
        else:
            result.unknowns.append("no evidence for the offering's target capabilities")
    else:
        service_fit = 0.5

    # --- need likelihood (§19.3) ---------------------------------------------
    # Two distinct opportunity shapes for a design/drafting-outsourcing offering:
    #   • no visible in-house capability → PRIMARY (they need a drafting partner)
    #   • has in-house capability        → SECONDARY (overflow / supplant work) —
    #     a real lead, positioned differently, NOT disqualified.
    # (Detected "in-house design" is often an outsourced partner's work presented as
    # the company's own, which is exactly why presence must not zero out need.)
    need = 0.5
    if offering.need_gap_capability_ids:
        gaps_present = [
            capability_by_id[cap_id]
            for cap_id in offering.need_gap_capability_ids
            if cap_id in capability_by_id
            and capability_by_id[cap_id].capability_status
            in (
                CapabilityStatus.CLAIMED,
                CapabilityStatus.EVIDENCED,
                CapabilityStatus.REPEATEDLY_EVIDENCED,
                CapabilityStatus.VERIFIED,
            )
        ]
        missing = [
            cap_id
            for cap_id in offering.need_gap_capability_ids
            if cap_id not in capability_by_id
        ]
        if gaps_present:
            need = 0.45  # secondary opportunity, not a disqualifier
            names = ", ".join(g.capability_label or g.capability_id for g in gaps_present)
            result.reasons.append(
                _reason(
                    "need_likelihood",
                    f"has in-house {names} — secondary target: position as overflow "
                    "capacity or to supplant their current drafting",
                    [f"capability:{g.id}" for g in gaps_present],
                )
            )
        elif missing:
            need = 0.7
            result.reasons.append(
                _reason(
                    "need_likelihood",
                    "no visible internal capability for: " + ", ".join(missing) + " — "
                    "primary drafting-partner opportunity (hypothesis, not a confirmed gap)",
                )
            )
            result.unknowns.append(
                "no reliable evidence of internal staffing for: " + ", ".join(missing)
            )

    # --- scale ---------------------------------------------------------------
    tier_estimate = size_by_concept.get(SizeConcept.CAPABILITY_TIER)
    tier = tier_estimate.band if tier_estimate else "unresolved"
    scale = _TIER_SCORES.get(tier, 0.4)
    if tier == "unresolved":
        result.unknowns.append("operating tier unresolved — insufficient size evidence")
    else:
        result.reasons.append(
            _reason("scale", f"operating tier {tier}", [f"size_estimate:{tier_estimate.id}"])
        )

    # --- geographic fit ------------------------------------------------------
    geo_codes = list(offering.geo_codes or [])
    location = organisation.primary_location
    org_codes: list[str] = []
    if location is not None and location.country:
        code = location.country.upper()
        if location.region:
            code = f"{code}-{location.region.upper()}"
        org_codes = [code]
    if not geo_codes:
        geographic_fit, geo_note = 0.7, "offering has no geographic restriction"
    elif org_codes and match_geography(
        org_codes, geo_codes, mode=GeographyMatchMode.OVERLAPS, include_unknown=False
    ):
        geographic_fit, geo_note = 1.0, f"located in target geography ({org_codes[0]})"
    elif org_codes:
        geographic_fit, geo_note = 0.2, f"outside target geography ({org_codes[0]})"
    else:
        geographic_fit, geo_note = 0.5, "location unknown"
        result.unknowns.append("no location on record — geographic fit is a guess")
    result.reasons.append(_reason("geographic_fit", geo_note))

    # --- negative archetypes (§19.4) ----------------------------------------
    negative_hit = [
        c
        for c in classifications
        if c.category_id in (offering.negative_archetype_ids or [])
        and c.assignment_type != "rejected"
    ]
    if negative_hit:
        result.risks.append(
            "matches a negative archetype for this offering: "
            + ", ".join(c.category_label or c.category_id for c in negative_hit)
        )

    # --- evidence quality (weak evidence lowers everything) ------------------
    if assertions:
        mean_confidence = sum(a.final_confidence for a in assertions) / len(assertions)
        evidence_quality = round(
            0.6 * mean_confidence + 0.4 * organisation.profile_completeness, 3
        )
        result.reasons.append(
            _reason(
                "evidence_quality",
                f"{len(assertions)} evidence-backed facts, mean confidence "
                f"{mean_confidence:.2f}, profile {organisation.profile_completeness:.0%}",
            )
        )
    else:
        evidence_quality = round(0.2 * organisation.profile_completeness, 3)
        result.unknowns.append("no reconciled facts yet — evidence quality is minimal")

    # --- accessibility (§20.2 priority order) --------------------------------
    contacts = [c for o in group for c in o.contact_points if c.public_business_contact]
    accessibility = 0.15
    for contact_type, score, label in _CONTACT_ROUTE_SCORES:
        matched = [c for c in contacts if c.contact_type == contact_type]
        if matched:
            accessibility = score
            result.reasons.append(
                _reason(
                    "accessibility",
                    f"best public route: {label}",
                    [f"contact:{matched[0].id}"],
                )
            )
            break
    else:
        result.unknowns.append("no public business contact route on record")
        result.next_action = {
            "type": "research",
            "text": "find a public business contact route (§20.2)",
        }

    # --- existing relationships (informational + risk, §19.4) ----------------
    edges = edges_for(session, organisation.id)
    if edges:
        result.relationship = 0.5
        conflict_types = {"customer_of", "partner_of", "supplier_to"}
        conflicts = [
            e for e in edges if e.kind == "relationship" and e.label in conflict_types
        ]
        if conflicts:
            result.risks.append(
                "existing commercial relationship(s): "
                + ", ".join(sorted({e.label for e in conflicts}))
                + " — check outreach is appropriate"
            )

    # --- timing (declared stub until M7, ADR-0015) ---------------------------
    result.timing = TIMING_STUB
    result.unknowns.append("timing signals land with M7 (news/events) — neutral for now")

    # --- combine --------------------------------------------------------------
    fit = (
        weights["industry_fit"] * industry_fit
        + weights["service_fit"] * service_fit
        + weights["need_likelihood"] * need
        + weights["scale"] * scale
        + weights["geographic_fit"] * geographic_fit
    )
    total_weight = sum(weights.values())
    fit = round(fit / total_weight if total_weight else 0.0, 3)
    if negative_hit:
        fit = round(fit * 0.3, 3)

    result.fit = fit
    result.evidence_quality = evidence_quality
    result.accessibility = accessibility
    base_priority = (
        PRIORITY_MIX["fit"] * fit
        + PRIORITY_MIX["timing"] * result.timing
        + PRIORITY_MIX["evidence"] * evidence_quality
        + PRIORITY_MIX["accessibility"] * accessibility
    )
    # Evidence quality also scales the whole: thin evidence can't produce a top lead.
    result.commercial_priority = round(base_priority * (0.5 + 0.5 * evidence_quality), 3)
    result.components = {
        "industry_fit": industry_fit,
        "service_fit": service_fit,
        "need_likelihood": need,
        "scale": scale,
        "geographic_fit": geographic_fit,
        "timing": result.timing,
        "evidence_quality": evidence_quality,
        "accessibility": accessibility,
        "weights": weights,
        "priority_mix": PRIORITY_MIX,
    }
    if result.next_action is None:
        result.next_action = {
            "type": "review",
            "text": "review reasons and unknowns; confirm fit before any outreach",
        }
    return result
