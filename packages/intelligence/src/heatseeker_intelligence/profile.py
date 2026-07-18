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

from heatseeker_intelligence import confidence as confidence_module
from heatseeker_intelligence.capabilities import capabilities_for, refresh_status
from heatseeker_intelligence.classifications import classifications_for
from heatseeker_intelligence.facts import NON_ASSERTION_PREDICATES, assertions_for
from heatseeker_intelligence.gaps import open_questions
from heatseeker_intelligence.models import FactStatus, Observation
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


def _facts_are_current(session: Session, canonical_id: str, group_ids: list[str]) -> bool:
    observation_rows = session.execute(
        select(Observation.id, Observation.predicate).where(
            Observation.subject_entity_id.in_(group_ids),
            Observation.normalisation_status != "rejected",
            Observation.predicate.not_in(NON_ASSERTION_PREDICATES),
        )
    ).all()
    expected: dict[str, set[str]] = {}
    for observation_id, predicate in observation_rows:
        expected.setdefault(predicate, set()).add(observation_id)
    assertions = assertions_for(session, canonical_id)
    if {assertion.predicate for assertion in assertions} != set(expected):
        return False
    return all(
        assertion.rule_version == confidence_module.RULE_VERSION
        and (
            set(assertion.supporting_observation_ids)
            | set(assertion.contradicting_observation_ids)
        )
        == expected[assertion.predicate]
        for assertion in assertions
    )


def assemble(session: Session, organisation_id: str) -> dict:
    """Full profile for one organisation, aggregated across its merge group."""
    identity = group_profile(session, organisation_id)
    group = identity["group"]
    group_ids = [o.id for o in group]
    canonical = identity["canonical"]

    # Derived records are caches over immutable observations. Reconcile only when their
    # evidence set changed (including M4 merge/reversal), avoiding write churn on reads.
    if not _facts_are_current(session, canonical.id, group_ids):
        from heatseeker_intelligence import facts as facts_module

        facts_module.reconcile_all(session, canonical.id)
    for capability in capabilities_for(session, group_ids):
        refresh_status(capability)
    from heatseeker_intelligence import gaps, sizing

    sizing.estimate_sizes(session, canonical.id)
    gaps.generate_for(session, canonical.id)

    assertions = assertions_for(session, canonical.id)
    classification_rows = classifications_for(session, group_ids)
    capability_rows = capabilities_for(session, group_ids)
    size_rows = estimates_for(session, group_ids)
    contact_rows = identity["contact_points"]
    evidence_observation_ids = {
        observation_id
        for assignment in classification_rows
        for observation_id in assignment.evidence_ids
    }
    evidence_observation_ids.update(
        entry.get("observation_id")
        for capability in capability_rows
        for entry in capability.evidence_ids
        if isinstance(entry, dict) and entry.get("observation_id")
    )
    evidence_observation_ids.update(
        observation_id
        for row in contact_rows
        for observation_id in row["item"].source_evidence_ids
    )
    evidence_observations = {
        observation.id: observation
        for observation in session.execute(
            select(Observation).where(Observation.id.in_(evidence_observation_ids))
        ).scalars()
    }
    document_ids = {
        a.best_evidence_document_id for a in assertions if a.best_evidence_document_id
    }
    document_ids.update(
        observation.source_document_id for observation in evidence_observations.values()
    )
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
    evidence_document_by_observation = {
        observation_id: documents.get(observation.source_document_id)
        for observation_id, observation in evidence_observations.items()
    }
    classification_evidence = {
        assignment.id: [
            evidence_document_by_observation[observation_id]
            for observation_id in assignment.evidence_ids
            if evidence_document_by_observation.get(observation_id) is not None
        ]
        for assignment in classification_rows
    }
    capability_evidence = {
        capability.id: [
            evidence_document_by_observation[entry["observation_id"]]
            for entry in capability.evidence_ids
            if isinstance(entry, dict)
            and evidence_document_by_observation.get(entry.get("observation_id")) is not None
        ]
        for capability in capability_rows
    }
    contact_evidence = {
        row["item"].id: list(
            {
                document.id: document
                for observation_id in row["item"].source_evidence_ids
                if (document := evidence_document_by_observation.get(observation_id)) is not None
            }.values()
        )
        for row in contact_rows
    }
    fact_documents = {
        assertion.id: documents.get(assertion.best_evidence_document_id)
        for assertion in assertions
        if assertion.best_evidence_document_id
    }
    size_evidence = {
        estimate.id: [
            fact_documents[basis["fact_assertion_id"]]
            for basis in estimate.basis
            if basis.get("fact_assertion_id") in fact_documents
        ]
        for estimate in size_rows
    }

    return {
        "identity": identity,
        "duplicate_warnings": _duplicate_warnings(session, group_ids),
        "facts": facts,
        "conflicts": conflicts,
        "classifications": classification_rows,
        "classification_evidence": classification_evidence,
        "capabilities": capability_rows,
        "capability_evidence": capability_evidence,
        "contact_evidence": contact_evidence,
        "contradicted_capabilities": [
            c for c in capability_rows if c.capability_status == "contradicted"
        ],
        "size_estimates": size_rows,
        "size_evidence": size_evidence,
        "fact_by_predicate": {fact["predicate"]: fact for fact in facts},
        "research_questions": open_questions(session, group_ids),
    }


def refresh(session: Session, organisation_id: str) -> None:
    """Recompute the derived layers after evidence changes."""
    from heatseeker_intelligence import facts as facts_module
    from heatseeker_intelligence import gaps, sizing

    group = merge_group(session, organisation_id)
    canonical = group[0]
    group_ids = [organisation.id for organisation in group]
    facts_module.reconcile_all(session, canonical.id)
    for capability in capabilities_for(session, group_ids):
        refresh_status(capability)
    sizing.estimate_sizes(session, canonical.id)
    gaps.generate_for(session, canonical.id)
