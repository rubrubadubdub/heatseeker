"""Project and participation helpers (spec §13.9-§13.10)."""

from datetime import datetime

from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.resolution import canonical_id
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from heatseeker_knowledge_graph.models import (
    ParticipationStatus,
    Project,
    ProjectParticipation,
)


def create_project(
    session: Session,
    name: str,
    *,
    status: str = "unknown",
    project_type_ids: list[str] | None = None,
    sector_ids: list[str] | None = None,
    location_id: str | None = None,
    geography_scope: dict | None = None,
    estimated_value: float | None = None,
    currency: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    expected_start_date: datetime | None = None,
    expected_end_date: datetime | None = None,
    description: str | None = None,
    evidence_ids: list[str] | None = None,
) -> Project:
    name = name.strip()
    if not name:
        raise ValueError("project name must not be blank")
    project = Project(
        name=name,
        status=status,
        project_type_ids=project_type_ids or [],
        sector_ids=sector_ids or [],
        location_id=location_id,
        geography_scope=geography_scope,
        estimated_value=estimated_value,
        currency=(currency or "").strip().upper() or None,
        start_date=start_date,
        end_date=end_date,
        expected_start_date=expected_start_date,
        expected_end_date=expected_end_date,
        description=(description or "").strip() or None,
        evidence_ids=evidence_ids or [],
    )
    session.add(project)
    session.flush()
    return project


def add_participation(
    session: Session,
    project_id: str,
    organisation_id: str,
    role_type: str,
    *,
    status: str = ParticipationStatus.UNCONFIRMED,
    confidence: float = 0.5,
    contract_value: float | None = None,
    evidence_ids: list[str] | None = None,
) -> ProjectParticipation:
    """Attach (or strengthen) one organisation role on a project.

    The organisation resolves to its canonical record; a repeat of the same
    (project, organisation, role) accumulates evidence instead of duplicating.
    """
    role_type = role_type.strip().lower()
    if not role_type:
        raise ValueError("role_type must not be blank")
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"project not found: {project_id}")
    organisation_id = canonical_id(session, organisation_id)

    existing = session.execute(
        select(ProjectParticipation).where(
            ProjectParticipation.project_id == project_id,
            ProjectParticipation.organisation_id == organisation_id,
            ProjectParticipation.role_type == role_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.evidence_ids = sorted(set(existing.evidence_ids) | set(evidence_ids or []))
        existing.confidence = max(existing.confidence, confidence)
        if contract_value is not None:
            existing.contract_value = contract_value
        if status != ParticipationStatus.UNCONFIRMED:
            existing.status = status
        existing.last_observed_at = utc_now()
        session.flush()
        return existing

    participation = ProjectParticipation(
        project_id=project_id,
        organisation_id=organisation_id,
        role_type=role_type,
        status=status,
        confidence=max(0.0, min(1.0, confidence)),
        contract_value=contract_value,
        evidence_ids=sorted(set(evidence_ids or [])),
    )
    session.add(participation)
    session.flush()
    return participation


def set_participation_status(
    session: Session, participation_id: str, status: str
) -> ProjectParticipation:
    participation = session.get(ProjectParticipation, participation_id)
    if participation is None:
        raise LookupError(f"participation not found: {participation_id}")
    if status not in list(ParticipationStatus):
        raise ValueError(f"unknown participation status: {status!r}")
    participation.status = status
    participation.last_observed_at = utc_now()
    session.flush()
    return participation


def list_projects(session: Session, *, query: str | None = None, limit: int = 200) -> list[Project]:
    stmt = (
        select(Project)
        .options(selectinload(Project.participations))
        .order_by(Project.updated_at.desc())
        .limit(limit)
    )
    if query:
        stmt = stmt.where(Project.name.like(f"%{query.strip()}%"))
    return list(session.execute(stmt).scalars().unique())


def get_project(session: Session, project_id: str) -> Project | None:
    return session.get(Project, project_id)


def participations_for_organisation(
    session: Session, entity_ids: list[str]
) -> list[ProjectParticipation]:
    return list(
        session.execute(
            select(ProjectParticipation)
            .options(selectinload(ProjectParticipation.project))
            .where(ProjectParticipation.organisation_id.in_(entity_ids))
            .order_by(ProjectParticipation.first_observed_at.desc())
        ).scalars()
    )
