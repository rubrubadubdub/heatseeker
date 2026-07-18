"""State-aware next-step guidance for the dashboard (hand-holding layer).

Reads actual pipeline state and produces an ordered checklist: what's done, what needs
a decision, and the single most useful next action. The granular pages stay untouched —
this is a map, not a replacement.
"""

from dataclasses import dataclass, field

from heatseeker_common.models import Job
from heatseeker_entity_resolution.models import EntityMatchCandidate, Organisation
from heatseeker_intelligence.models import FactAssertion, ResearchQuestion
from heatseeker_knowledge_graph.models import Project, Relationship
from heatseeker_lead_intelligence.models import AccountOpportunity, Offering
from heatseeker_source_registry.models import SourceDefinition, SourceDocument
from heatseeker_source_registry.scopes import active_scope
from sqlalchemy import func, select
from sqlalchemy.orm import Session


@dataclass(slots=True)
class Step:
    key: str
    title: str
    detail: str
    href: str
    action_label: str
    state: str  # done | todo | attention
    count: int = 0
    extra_links: list[tuple[str, str]] = field(default_factory=list)


def _count(session: Session, stmt) -> int:
    return session.execute(stmt).scalar_one()


def next_steps(session: Session) -> list[Step]:
    sources_active = _count(
        session,
        select(func.count(SourceDefinition.id)).where(
            SourceDefinition.lifecycle_status == "active"
        ),
    )
    scope = active_scope(session)
    documents = _count(session, select(func.count(SourceDocument.id)))
    organisations = _count(
        session,
        select(func.count(Organisation.id)).where(Organisation.status != "merged"),
    )
    queue_open = _count(
        session,
        select(func.count(EntityMatchCandidate.id)).where(
            EntityMatchCandidate.resolution.is_(None)
        ),
    )
    conflicted = _count(
        session,
        select(func.count(FactAssertion.id)).where(FactAssertion.status == "conflicted"),
    )
    questions = _count(
        session,
        select(func.count(ResearchQuestion.id)).where(ResearchQuestion.status == "open"),
    )
    failed_jobs = _count(
        session, select(func.count(Job.id)).where(Job.status == "failed")
    )
    connections = _count(
        session,
        select(func.count(Relationship.id)).where(Relationship.status == "active"),
    ) + _count(session, select(func.count(Project.id)))
    offerings = _count(
        session, select(func.count(Offering.id)).where(Offering.status == "active")
    )
    scored_leads = _count(
        session,
        select(func.count(AccountOpportunity.id)).where(
            AccountOpportunity.opportunity_stage != "suppressed"
        ),
    )

    steps = [
        Step(
            key="sources",
            title="Activate collectable sources",
            detail=(
                f"{sources_active} active source{'s' if sources_active != 1 else ''}. "
                "Pack seeds sync automatically; review and activate policy-cleared ones."
            ),
            href="/sources",
            action_label="Review sources",
            state="done" if sources_active else "todo",
            count=sources_active,
            extra_links=[("Scout new sources with AI", "/source-scout")],
        ),
        Step(
            key="scope",
            title="Set your research scope",
            detail=(
                f"Active scope: {scope.name}." if scope else "No active scope — "
                "collection and imports won't be geographically filtered."
            ),
            href="/scopes",
            action_label="Choose scope",
            state="done" if scope else "todo",
        ),
        Step(
            key="evidence",
            title="Gather evidence",
            detail=(
                f"{documents} evidence document{'s' if documents != 1 else ''} stored. "
                "The worker's autopilot collects from active sources on its own; "
                "'Advance pipeline' nudges everything through immediately."
            ),
            href="/evidence",
            action_label="Browse evidence",
            state="done" if documents else "todo",
            count=documents,
        ),
        Step(
            key="population",
            title="Build the company population",
            detail=(
                f"{organisations} organisation{'s' if organisations != 1 else ''} on file. "
                "The fastest start: import an official dataset (ABR/NZBN CSV) on the "
                "Discovery page."
            ),
            href="/discovery",
            action_label="Import a dataset",
            state="done" if organisations else "todo",
            count=organisations,
        ),
        Step(
            key="resolution",
            title="Decide duplicate candidates",
            detail=(
                f"{queue_open} pair{'s' if queue_open != 1 else ''} waiting for a human "
                "decision — nothing merges by itself."
                if queue_open
                else "Queue is clear."
            ),
            href="/resolution",
            action_label="Open resolution queue",
            state="attention" if queue_open else "done",
            count=queue_open,
        ),
        Step(
            key="connections",
            title="Map projects & relationships",
            detail=(
                f"{connections} project{'s' if connections != 1 else ''}/relationship"
                "s recorded — shared projects and typed relationships make companies "
                "explorable as a network."
                if connections
                else "No projects or relationships yet. Once you have companies, connect "
                "them: add a project and its participants, or record a relationship on "
                "a company profile."
            ),
            href="/projects",
            action_label="Open projects",
            state="done" if connections or not organisations else "todo",
            count=connections,
        ),
        Step(
            key="conflicts",
            title="Review conflicting facts",
            detail=(
                f"{conflicted} fact{'s' if conflicted != 1 else ''} where sources disagree."
                if conflicted
                else "No conflicts right now."
            ),
            href="/entities",
            action_label="Open entities",
            state="attention" if conflicted else "done",
            count=conflicted,
        ),
        Step(
            key="gaps",
            title="Answer research gaps",
            detail=(
                f"{questions} open question{'s' if questions != 1 else ''} the system "
                "can't answer from current evidence."
                if questions
                else "No open research questions."
            ),
            href="/entities",
            action_label="Open entities",
            state="todo" if questions else "done",
            count=questions,
        ),
    ]
    steps.append(
        Step(
            key="leads",
            title="Define an offering & build the lead queue",
            detail=(
                f"{scored_leads} lead{'s' if scored_leads != 1 else ''} scored across "
                f"{offerings} offering{'s' if offerings != 1 else ''} — ranked, "
                "explained, exportable to XLSX."
                if offerings
                else "Tell Heatseeker what you sell; it ranks every known company "
                "against it with evidence-cited reasons."
            ),
            href="/leads",
            action_label="Open leads",
            state="done" if offerings or not organisations else "todo",
            count=scored_leads,
        )
    )
    if failed_jobs:
        steps.append(
            Step(
                key="failed_jobs",
                title="Investigate failed jobs",
                detail=f"{failed_jobs} job{'s' if failed_jobs != 1 else ''} failed.",
                href="/jobs?status=failed",
                action_label="Open jobs",
                state="attention",
                count=failed_jobs,
            )
        )
    return steps


def primary_step(steps: list[Step]) -> Step | None:
    """The single most useful thing to do now: first attention, else first todo."""
    for state in ("attention", "todo"):
        for step in steps:
            if step.state == state:
                return step
    return None
