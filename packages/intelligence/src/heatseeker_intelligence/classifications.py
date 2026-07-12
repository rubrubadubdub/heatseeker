"""Multi-axis, explainable classification (spec §15).

Deterministic rules only in M5 (AI rubric classification is an M11 seam). Taxonomies
come from industry packs; the one fixed axis is the spec's business-model list (§15.1),
which is industry-agnostic. Every assignment records its evidence observations and how
it was assigned — claimed vs inferred stays visible (§15.3).
"""

from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence.models import AssignmentType, ClassificationAssignment
from heatseeker_intelligence.observations import (
    PREDICATE_ARCHETYPE_CLAIM,
    PREDICATE_SERVICE_CLAIM,
)

CLASSIFIER_VERSION = "classify/0.1"

# Fixed spec axis (§15.1) — generic across industries, so it may live in core.
BUSINESS_MODEL_AXIS = "business_model"
BUSINESS_MODELS = (
    "contractor",
    "consultant",
    "manufacturer",
    "distributor",
    "rental",
    "labour_provider",
    "principal_contractor",
    "asset_owner",
    "software_provider",
    "association",
    "training_provider",
    "regulator",
    "buyer",
    "intermediary",
)

# Source category → how much to trust the claim and what kind of assignment it is.
_CATEGORY_ASSIGNMENT: dict[str, tuple[str, float]] = {
    "government_registry": (AssignmentType.REGISTERED, 0.9),
    "regulator": (AssignmentType.REGISTERED, 0.9),
    "bulk_dataset": (AssignmentType.REGISTERED, 0.85),
    "company_website": (AssignmentType.SELF_DESCRIBED, 0.7),
    "industry_association": (AssignmentType.OBSERVED, 0.6),
    "directory": (AssignmentType.OBSERVED, 0.4),  # directories are weak signals (§39.1)
}
_DEFAULT_ASSIGNMENT = (AssignmentType.OBSERVED, 0.5)


def assignment_type_for_category(source_category: str | None) -> tuple[str, float]:
    return _CATEGORY_ASSIGNMENT.get(source_category or "", _DEFAULT_ASSIGNMENT)


def assign(
    session: Session,
    entity_id: str,
    *,
    pack_id: str,
    taxonomy_id: str,
    category_id: str,
    category_label: str = "",
    assignment_type: str = AssignmentType.OBSERVED,
    confidence: float = 0.5,
    evidence_ids: list[str] | None = None,
    classifier_version: str = CLASSIFIER_VERSION,
) -> ClassificationAssignment:
    """Create or strengthen one classification; evidence accumulates, never replaces."""
    existing = session.execute(
        select(ClassificationAssignment).where(
            ClassificationAssignment.entity_id == entity_id,
            ClassificationAssignment.pack_id == pack_id,
            ClassificationAssignment.taxonomy_id == taxonomy_id,
            ClassificationAssignment.category_id == category_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.assignment_type == AssignmentType.REJECTED:
            return existing  # a human said no — evidence does not silently reopen it
        merged = sorted(set(existing.evidence_ids) | set(evidence_ids or []))
        existing.evidence_ids = merged
        # Human confirmation outranks everything; otherwise keep the strongest signal.
        if assignment_type == AssignmentType.HUMAN_CONFIRMED:
            existing.assignment_type = assignment_type
            existing.confidence = max(existing.confidence, confidence, 0.9)
        else:
            existing.confidence = max(existing.confidence, confidence)
            if existing.assignment_type != AssignmentType.HUMAN_CONFIRMED:
                order = [t.value for t in AssignmentType]
                if order.index(assignment_type) < order.index(existing.assignment_type):
                    existing.assignment_type = assignment_type
        existing.status = "active"
        existing.classifier_version = classifier_version
        existing.updated_at = utc_now()
        session.flush()
        return existing

    assignment = ClassificationAssignment(
        entity_id=entity_id,
        pack_id=pack_id,
        taxonomy_id=taxonomy_id,
        category_id=category_id,
        category_label=category_label,
        assignment_type=assignment_type,
        confidence=max(0.0, min(1.0, confidence)),
        evidence_ids=sorted(set(evidence_ids or [])),
        classifier_version=classifier_version,
        valid_from=utc_now(),
    )
    session.add(assignment)
    session.flush()
    return assignment


def reject(
    session: Session, assignment_id: str, *, actor: str = "user"
) -> ClassificationAssignment:
    assignment = session.get(ClassificationAssignment, assignment_id)
    if assignment is None:
        raise LookupError(f"classification not found: {assignment_id}")
    assignment.assignment_type = AssignmentType.REJECTED
    assignment.status = "retracted"
    assignment.valid_to = utc_now()
    assignment.updated_at = utc_now()
    session.flush()
    return assignment


def classify_from_observations(
    session: Session,
    entity_id: str,
    observations: list,
    *,
    pack_id: str,
    source_category: str | None,
    known_service_ids: dict[str, str] | None = None,
    known_archetype_ids: dict[str, str] | None = None,
) -> list[ClassificationAssignment]:
    """Deterministic rule: explicit service/archetype claims become classifications.

    `known_*_ids` map valid pack category ids to display labels; claims outside the
    pack vocabulary are ignored rather than guessed (§40 abstain-over-fabricate).
    """
    assignment_type, confidence = assignment_type_for_category(source_category)
    results = []
    for observation in observations:
        value = observation.object_value
        category_id = value if isinstance(value, str) else None
        if observation.predicate == PREDICATE_SERVICE_CLAIM and known_service_ids:
            if category_id in known_service_ids:
                results.append(
                    assign(
                        session,
                        entity_id,
                        pack_id=pack_id,
                        taxonomy_id="service_taxonomy",
                        category_id=category_id,
                        category_label=known_service_ids[category_id],
                        assignment_type=assignment_type,
                        confidence=confidence,
                        evidence_ids=[observation.id],
                    )
                )
        elif (
            observation.predicate == PREDICATE_ARCHETYPE_CLAIM
            and known_archetype_ids
            and category_id in known_archetype_ids
        ):
            results.append(
                assign(
                    session,
                    entity_id,
                    pack_id=pack_id,
                    taxonomy_id="company_archetypes",
                    category_id=category_id,
                    category_label=known_archetype_ids[category_id],
                    assignment_type=assignment_type,
                    confidence=confidence,
                    evidence_ids=[observation.id],
                )
            )
    return results


def classifications_for(
    session: Session, entity_ids: list[str], *, include_retracted: bool = False
) -> list[ClassificationAssignment]:
    stmt = select(ClassificationAssignment).where(
        ClassificationAssignment.entity_id.in_(entity_ids)
    )
    if not include_retracted:
        stmt = stmt.where(ClassificationAssignment.status == "active")
    return list(
        session.execute(
            stmt.order_by(
                ClassificationAssignment.taxonomy_id, ClassificationAssignment.category_id
            )
        ).scalars()
    )
