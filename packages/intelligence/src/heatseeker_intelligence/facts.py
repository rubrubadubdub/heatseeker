"""Fact reconciliation: observations in, one inspectable assertion per predicate out.

Contradicting observations are preserved on the assertion, never deleted (§17.6). An
entity with no observations for a predicate gets no assertion at all — missing stays
missing (§6.3).
"""

from collections import defaultdict

from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.resolution import merge_group
from heatseeker_source_registry.models import SourceDefinition, SourceDocument, SourceRelationship
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from heatseeker_intelligence import confidence as conf
from heatseeker_intelligence.models import FactAssertion, FactStatus, Observation
from heatseeker_intelligence.observations import observations_for, value_key

# Contested share of observations at which a fact is flagged conflicted (§17.6).
CONFLICT_RATIO = 1 / 3
NON_ASSERTION_PREDICATES = {"service_claim", "archetype_claim", "social_profile"}
DEPENDENT_SOURCE_RELATIONSHIPS = {
    "copied_from",
    "derived_from",
    "mirror_of",
    "owned_by",
    "same_owner_as",
    "syndicates_from",
}


def _document_sources(
    session: Session, observations: list[Observation]
) -> dict[str, tuple[SourceDocument, SourceDefinition]]:
    document_ids = {o.source_document_id for o in observations}
    rows = session.execute(
        select(SourceDocument, SourceDefinition)
        .join(SourceDefinition, SourceDocument.source_definition_id == SourceDefinition.id)
        .where(SourceDocument.id.in_(document_ids))
    ).all()
    return {document.id: (document, source) for document, source in rows}


def _independence_keys(session: Session, source_ids: set[str]) -> dict[str, str]:
    """Collapse known copied/owned/syndicated sources into one corroboration origin."""

    parent = {source_id: source_id for source_id in source_ids}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    if source_ids:
        relationships = session.scalars(
            select(SourceRelationship).where(
                or_(
                    SourceRelationship.source_definition_id.in_(source_ids),
                    SourceRelationship.related_source_definition_id.in_(source_ids),
                ),
                SourceRelationship.relationship_type.in_(DEPENDENT_SOURCE_RELATIONSHIPS),
                SourceRelationship.confidence >= 0.5,
                or_(
                    SourceRelationship.valid_to.is_(None),
                    SourceRelationship.valid_to >= utc_now(),
                ),
            )
        ).all()
        for relationship in relationships:
            union(
                relationship.source_definition_id,
                relationship.related_source_definition_id,
            )
    return {source_id: find(source_id) for source_id in source_ids}


def reconcile(
    session: Session, organisation_id: str, predicate: str
) -> FactAssertion | None:
    """Recompute the assertion for one (entity, predicate) from the whole merge group."""
    group_ids = [o.id for o in merge_group(session, organisation_id)]
    canonical_id = group_ids[0]
    observations = [
        o
        for o in observations_for(session, group_ids, predicate)
        if o.normalisation_status != "rejected"
    ]

    existing = session.execute(
        select(FactAssertion).where(
            FactAssertion.subject_entity_id == canonical_id,
            FactAssertion.predicate == predicate,
        )
    ).scalar_one_or_none()

    # These predicates are multi-valued evidence inputs to classification/capability
    # ledgers. Treating different services as contradictory scalar facts is incorrect.
    if predicate in NON_ASSERTION_PREDICATES:
        if existing is not None:
            session.delete(existing)
            session.flush()
        return None

    if not observations:
        # Missing ≠ false: no evidence means no assertion. Drop a stale one if the
        # observations behind it were rejected since.
        if existing is not None:
            session.delete(existing)
            session.flush()
        return None

    # Group by canonical value. Authority leads; independent corroboration can strengthen
    # it but cannot let a pile of weak directories outvote a primary registry.
    sources_by_document = _document_sources(session, observations)
    independence_keys = _independence_keys(
        session, {source.id for _document, source in sources_by_document.values()}
    )
    groups: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        groups[value_key(observation.object_value)].append(observation)

    def _group_rank(items: list[Observation]) -> tuple:
        sources = {
            sources_by_document[o.source_document_id][1]
            for o in items
            if o.source_document_id in sources_by_document
        }
        best_authority = max(
            (
                conf.authority_score(source.authority_tier, source.source_category, predicate)
                for source in sources
            ),
            default=0.0,
        )
        independent_origins = {
            independence_keys.get(source.id, source.id) for source in sources
        }
        authority_with_corroboration = best_authority * conf.corroboration_score(
            len(independent_origins)
        )
        extraction = max(o.extraction_confidence for o in items)
        newest = max(o.observed_at for o in items)
        return (authority_with_corroboration, extraction, len(sources), newest)

    winning_key = max(groups, key=lambda key: _group_rank(groups[key]))
    supporting = groups[winning_key]
    contradicting = [o for key, items in groups.items() if key != winning_key for o in items]

    supporting_sources = {
        independence_keys.get(
            sources_by_document[o.source_document_id][1].id,
            sources_by_document[o.source_document_id][1].id,
        )
        for o in supporting
        if o.source_document_id in sources_by_document
    }
    contradicting_sources = {
        independence_keys.get(
            sources_by_document[o.source_document_id][1].id,
            sources_by_document[o.source_document_id][1].id,
        )
        for o in contradicting
        if o.source_document_id in sources_by_document
    }
    newest = max(o.observed_at for o in supporting)
    human_verified = any(o.human_verified for o in supporting)

    def _best_authority() -> tuple[float, str | None]:
        best, best_document = 0.0, None
        for observation in supporting:
            pair = sources_by_document.get(observation.source_document_id)
            if pair is None:
                continue
            _document, source = pair
            score = conf.authority_score(source.authority_tier, source.source_category, predicate)
            if score > best:
                best, best_document = score, observation.source_document_id
        return best, best_document

    authority, best_document_id = _best_authority()
    breakdown = conf.ConfidenceBreakdown(
        authority=authority,
        extraction=max(o.extraction_confidence for o in supporting),
        match=0.95 if human_verified else 0.85,
        freshness=conf.freshness_score(predicate, newest),
        corroboration=conf.corroboration_score(len(supporting_sources)),
        contradiction=conf.contradiction_score(
            len(supporting_sources), len(contradicting_sources)
        ),
    )
    final = breakdown.final

    source_total = len(supporting_sources) + len(contradicting_sources)
    contested = bool(contradicting_sources) and source_total > 0 and (
        len(contradicting_sources) / source_total >= CONFLICT_RATIO
    )
    stale = breakdown.freshness < conf.STALE_THRESHOLD
    if contested:
        status = FactStatus.CONFLICTED
    elif stale:
        status = FactStatus.STALE
    elif (final >= 0.8 and len(supporting_sources) >= 2) or (human_verified and final >= 0.6):
        status = FactStatus.CONFIRMED
    elif final >= 0.55:
        status = FactStatus.PROBABLE
    elif final >= 0.3:
        status = FactStatus.POSSIBLE
    else:
        status = FactStatus.UNKNOWN

    assertion = existing or FactAssertion(subject_entity_id=canonical_id, predicate=predicate)
    assertion.value = supporting[-1].object_value
    assertion.status = status
    assertion.authority_score = breakdown.authority
    assertion.extraction_score = breakdown.extraction
    assertion.match_score = breakdown.match
    assertion.freshness_score = breakdown.freshness
    assertion.corroboration_score = breakdown.corroboration
    assertion.contradiction_score = breakdown.contradiction
    assertion.final_confidence = final
    assertion.confidence_vocabulary = conf.vocabulary(
        final, conflicted=contested, stale=stale, human_verified=human_verified
    )
    assertion.supporting_observation_ids = [o.id for o in supporting]
    assertion.contradicting_observation_ids = [o.id for o in contradicting]
    assertion.independent_source_count = len(supporting_sources)
    assertion.best_evidence_document_id = best_document_id
    assertion.last_observed_at = newest
    assertion.rule_version = conf.RULE_VERSION
    assertion.updated_at = utc_now()
    if existing is None:
        session.add(assertion)
    session.flush()
    return assertion


def reconcile_all(session: Session, organisation_id: str) -> list[FactAssertion]:
    """Reconcile every predicate that has observations for this entity's merge group."""
    group_ids = [o.id for o in merge_group(session, organisation_id)]
    predicates = {
        row
        for row in session.execute(
            select(Observation.predicate)
            .where(Observation.subject_entity_id.in_(group_ids))
            .distinct()
        ).scalars()
    }
    results = []
    for predicate in sorted(predicates):
        assertion = reconcile(session, organisation_id, predicate)
        if assertion is not None:
            results.append(assertion)
    return results


def assertions_for(session: Session, organisation_id: str) -> list[FactAssertion]:
    canonical = merge_group(session, organisation_id)[0]
    return list(
        session.execute(
            select(FactAssertion)
            .where(FactAssertion.subject_entity_id == canonical.id)
            .order_by(FactAssertion.predicate)
        ).scalars()
    )
