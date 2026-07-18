"""Research-question generation from missing, conflicted, and stale intelligence (§18.7).

Gaps are how "missing stays missing" becomes actionable: instead of fabricating a value,
the system asks a question a human (or a later research run) can answer.
"""

from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.resolution import merge_group
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence.capabilities import capabilities_for
from heatseeker_intelligence.facts import assertions_for
from heatseeker_intelligence.models import (
    CapabilityStatus,
    FactStatus,
    QuestionStatus,
    ResearchQuestion,
)

GAP_RULE_VERSION = "gaps/0.1"


def _open_types(session: Session, entity_id: str) -> set[str]:
    return {
        row
        for row in session.execute(
            select(ResearchQuestion.question_type).where(
                ResearchQuestion.entity_id == entity_id,
                ResearchQuestion.status.in_([QuestionStatus.OPEN, QuestionStatus.IN_PROGRESS]),
            )
        ).scalars()
    }


def _ask(
    session: Session,
    entity_id: str,
    question_type: str,
    text: str,
    reason: str,
    priority: float,
    existing_types: set[str],
) -> ResearchQuestion | None:
    if question_type in existing_types:
        return None
    question = ResearchQuestion(
        entity_id=entity_id,
        question_type=question_type,
        question_text=text,
        reason=reason,
        priority=round(max(0.0, min(1.0, priority)), 2),
        generated_by="system",
    )
    session.add(question)
    existing_types.add(question_type)
    return question


def generate_for(session: Session, organisation_id: str) -> list[ResearchQuestion]:
    """(Re)generate gap questions for one organisation. Deduped per open question type."""
    group = merge_group(session, organisation_id)
    canonical = group[0]
    name = canonical.canonical_name
    existing_types = _open_types(session, canonical.id)
    needed_types: set[str] = set()
    created: list[ResearchQuestion] = []

    def ask(question_type, text, reason, priority):
        needed_types.add(question_type)
        question = _ask(
            session, canonical.id, question_type, text, reason, priority, existing_types
        )
        if question is not None:
            created.append(question)

    if not any(o.identifiers for o in group):
        ask(
            "missing_identifier",
            f"What is the registration identifier (ABN/NZBN/…) for {name}?",
            "no registration identifier on record — entity matching and registry "
            "corroboration are blocked without one",
            0.8,
        )
    if not any(o.domains for o in group):
        ask(
            "missing_domain",
            f"Does {name} have a website?",
            "no website domain on record — self-described services and contacts unknown",
            0.7,
        )
    if not any(o.primary_location_id for o in group):
        ask(
            "missing_location",
            f"Where does {name} operate from?",
            "no location on record — geographic scoping impossible",
            0.6,
        )
    if not any(o.contact_points for o in group):
        ask(
            "missing_contact",
            f"What public business contact routes exist for {name}?",
            "no contact points on record",
            0.5,
        )

    capability_rows = capabilities_for(session, [o.id for o in group])
    if not capability_rows:
        ask(
            "no_capability_evidence",
            f"Which services does {name} actually provide?",
            "no capability evidence on record",
            0.6,
        )
    for capability in capability_rows:
        if capability.capability_status == CapabilityStatus.CONTRADICTED:
            label = capability.capability_label or capability.capability_id
            ask(
                f"contradicted_capability:{capability.capability_id}",
                f"Does {name} really provide '{label}'? Evidence conflicts.",
                "capability has contradicting observations (§17.6)",
                0.75,
            )

    for assertion in assertions_for(session, canonical.id):
        if assertion.status == FactStatus.CONFLICTED:
            ask(
                f"conflicted_fact:{assertion.predicate}",
                f"Sources disagree about '{assertion.predicate}' for {name} — which is right?",
                f"{len(assertion.contradicting_observation_ids)} contradicting vs "
                f"{len(assertion.supporting_observation_ids)} supporting observations",
                0.8,
            )
        elif assertion.status == FactStatus.STALE:
            ask(
                f"stale_fact:{assertion.predicate}",
                f"Is '{assertion.predicate}' for {name} still current?",
                f"freshness score {assertion.freshness_score} below threshold",
                0.55,
            )

    obsolete = session.scalars(
        select(ResearchQuestion).where(
            ResearchQuestion.entity_id == canonical.id,
            ResearchQuestion.generated_by == "system",
            ResearchQuestion.status.in_([QuestionStatus.OPEN, QuestionStatus.IN_PROGRESS]),
        )
    ).all()
    for question in obsolete:
        if question.question_type in needed_types:
            continue
        question.status = QuestionStatus.RESOLVED
        question.resolution = {
            "by": "system",
            "note": "underlying evidence gap is no longer present",
            "at": utc_now().isoformat(),
        }
        question.updated_at = utc_now()

    session.flush()
    return created


def open_questions(session: Session, entity_ids: list[str]) -> list[ResearchQuestion]:
    return list(
        session.execute(
            select(ResearchQuestion)
            .where(
                ResearchQuestion.entity_id.in_(entity_ids),
                ResearchQuestion.status.in_(
                    [QuestionStatus.OPEN, QuestionStatus.IN_PROGRESS]
                ),
            )
            .order_by(ResearchQuestion.priority.desc(), ResearchQuestion.created_at)
        ).scalars()
    )


def resolve_question(
    session: Session,
    question_id: str,
    *,
    status: str,
    note: str | None = None,
    actor: str = "user",
) -> ResearchQuestion:
    if status not in (QuestionStatus.RESOLVED, QuestionStatus.DISMISSED):
        raise ValueError("status must be resolved or dismissed")
    question = session.get(ResearchQuestion, question_id)
    if question is None:
        raise LookupError(f"research question not found: {question_id}")
    question.status = status
    question.resolution = {"by": actor, "note": note or "", "at": utc_now().isoformat()}
    question.updated_at = utc_now()
    session.flush()
    return question
