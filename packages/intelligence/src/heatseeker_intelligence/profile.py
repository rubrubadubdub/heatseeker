"""Company profile assembly (spec §18): one call feeding both the UI and the JSON API.

Every field row carries its confidence components, source counts, contradiction counts,
and a link to the best evidence document — the §18.3 evidence summary. Unknown things
appear as research gaps, not fabricated values.
"""

from heatseeker_entity_resolution.models import EntityMatchCandidate
from heatseeker_entity_resolution.resolution import group_profile, merge_group
from heatseeker_source_registry.models import SourceDocument
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from heatseeker_intelligence.capabilities import capabilities_for
from heatseeker_intelligence.classifications import classifications_for
from heatseeker_intelligence.facts import assertions_for
from heatseeker_intelligence.gaps import open_questions
from heatseeker_intelligence.models import FactStatus
from heatseeker_intelligence.sizing import estimates_for


def _duplicate_warnings(session: Session, entity_ids: list[str]) -> list[EntityMatchCandidate]:
    return list(
        session.execute(
            select(EntityMatchCandidate)
            .where(
                or_(
                    EntityMatchCandidate.organisation_a_id.in_(entity_ids),
                    EntityMatchCandidate.organisation_b_id.in_(entity_ids),
                ),
                EntityMatchCandidate.resolution.is_(None),
            )
            .order_by(EntityMatchCandidate.score.desc())
        ).scalars()
    )


def assemble(session: Session, organisation_id: str) -> dict:
    """Full profile for one organisation, aggregated across its merge group."""
    identity = group_profile(session, organisation_id)
    group = identity["group"]
    group_ids = [o.id for o in group]
    canonical = identity["canonical"]

    assertions = assertions_for(session, canonical.id)
    document_ids = {
        a.best_evidence_document_id for a in assertions if a.best_evidence_document_id
    }
    documents = {
        d.id: d
        for d in session.execute(
            select(SourceDocument).where(SourceDocument.id.in_(document_ids))
        ).scalars()
    }

    facts = [
        {
            "predicate": assertion.predicate,
            "value": assertion.value,
            "status": assertion.status,
            "confidence": assertion.final_confidence,
            "confidence_vocabulary": assertion.confidence_vocabulary,
            "components": {
                "authority": assertion.authority_score,
                "extraction": assertion.extraction_score,
                "match": assertion.match_score,
                "freshness": assertion.freshness_score,
                "corroboration": assertion.corroboration_score,
                "contradiction": assertion.contradiction_score,
            },
            "supporting_count": len(assertion.supporting_observation_ids),
            "contradicting_count": len(assertion.contradicting_observation_ids),
            "independent_source_count": assertion.independent_source_count,
            "best_evidence_document": documents.get(assertion.best_evidence_document_id),
            "last_observed_at": assertion.last_observed_at,
            "rule_version": assertion.rule_version,
        }
        for assertion in assertions
    ]

    conflicts = [f for f in facts if f["status"] == FactStatus.CONFLICTED]
    capability_rows = capabilities_for(session, group_ids)

    return {
        "identity": identity,
        "duplicate_warnings": _duplicate_warnings(session, group_ids),
        "facts": facts,
        "conflicts": conflicts,
        "classifications": classifications_for(session, group_ids),
        "capabilities": capability_rows,
        "contradicted_capabilities": [
            c for c in capability_rows if c.capability_status == "contradicted"
        ],
        "size_estimates": estimates_for(session, group_ids),
        "research_questions": open_questions(session, group_ids),
    }


def refresh(session: Session, organisation_id: str) -> None:
    """Recompute the derived layers after evidence changes."""
    from heatseeker_intelligence import facts as facts_module
    from heatseeker_intelligence import gaps, sizing

    canonical = merge_group(session, organisation_id)[0]
    facts_module.reconcile_all(session, canonical.id)
    sizing.estimate_sizes(session, canonical.id)
    gaps.generate_for(session, canonical.id)
