"""Project and participation helpers (spec §13.9-§13.10)."""

from datetime import datetime

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.models import Location, Organisation
from heatseeker_entity_resolution.resolution import canonical_id
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from heatseeker_knowledge_graph.evidence import validate_confidence, validate_evidence_ids
from heatseeker_knowledge_graph.models import (
    ParticipationStatus,
    Project,
    ProjectParticipation,
    ProjectStatus,
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
    actor: str = "user",
) -> Project:
    name = name.strip()
    if not name:
        raise ValueError("project name must not be blank")
    if status not in [item.value for item in ProjectStatus]:
        raise ValueError(f"unknown project status: {status!r}")
    if estimated_value is not None and estimated_value < 0:
        raise ValueError("estimated_value must not be negative")
    if estimated_value is not None and not (currency or "").strip():
        raise ValueError("currency is required when estimated_value is supplied")
    if location_id is not None and session.get(Location, location_id) is None:
        raise LookupError(f"location not found: {location_id}")
    for label, value in (
        ("start_date", start_date),
        ("end_date", end_date),
        ("expected_start_date", expected_start_date),
        ("expected_end_date", expected_end_date),
    ):
        if value is not None and value.tzinfo is None:
            raise ValueError(f"project {label} must include a timezone")
    if start_date and end_date and end_date < start_date:
        raise ValueError("project end_date must not precede start_date")
    if expected_start_date and expected_end_date and expected_end_date < expected_start_date:
        raise ValueError("expected_end_date must not precede expected_start_date")
    evidence = validate_evidence_ids(session, evidence_ids)
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
        evidence_ids=evidence,
    )
    session.add(project)
    session.flush()
    audit.record(
        session,
        actor,
        "project.created",
        "project",
        project.id,
        {"name": project.name, "status": project.status},
    )
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
    actor: str = "user",
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
    if status not in [item.value for item in ParticipationStatus]:
        raise ValueError(f"unknown participation status: {status!r}")
    if status in (ParticipationStatus.HISTORICAL, ParticipationStatus.RETRACTED):
        raise ValueError("new participation cannot start as historical or retracted")
    if contract_value is not None and contract_value < 0:
        raise ValueError("contract_value must not be negative")
    organisation_id = canonical_id(session, organisation_id)
    if session.get(Organisation, organisation_id) is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    evidence = validate_evidence_ids(session, evidence_ids)

    existing = session.execute(
        select(ProjectParticipation).where(
            ProjectParticipation.project_id == project_id,
            ProjectParticipation.organisation_id == organisation_id,
            ProjectParticipation.role_type == role_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status in (ParticipationStatus.HISTORICAL, ParticipationStatus.RETRACTED):
            raise ValueError(f"participation is {existing.status}; history is immutable")
        combined_evidence = sorted(set(existing.evidence_ids) | set(evidence))
        validated_confidence = validate_confidence(
            confidence, has_evidence=bool(combined_evidence)
        )
        if (
            status in (ParticipationStatus.PROBABLE, ParticipationStatus.CONFIRMED)
            and not combined_evidence
        ):
            raise ValueError(f"{status} participation requires evidence")
        existing.evidence_ids = combined_evidence
        existing.confidence = max(existing.confidence, validated_confidence)
        if contract_value is not None:
            existing.contract_value = contract_value
        if status != ParticipationStatus.UNCONFIRMED:
            existing.status = status
        existing.last_observed_at = utc_now()
        session.flush()
        audit.record(
            session,
            actor,
            "project.participation_updated",
            "project_participation",
            existing.id,
            {"status": existing.status, "evidence_count": len(existing.evidence_ids)},
        )
        return existing

    if status in (ParticipationStatus.PROBABLE, ParticipationStatus.CONFIRMED) and not evidence:
        raise ValueError(f"{status} participation requires evidence")
    validated_confidence = validate_confidence(confidence, has_evidence=bool(evidence))
    participation = ProjectParticipation(
        project_id=project_id,
        organisation_id=organisation_id,
        role_type=role_type,
        status=status,
        confidence=validated_confidence,
        contract_value=contract_value,
        evidence_ids=evidence,
    )
    session.add(participation)
    session.flush()
    audit.record(
        session,
        actor,
        "project.participation_added",
        "project_participation",
        participation.id,
        {"project_id": project_id, "status": status, "role_type": role_type},
    )
    return participation


def set_participation_status(
    session: Session,
    participation_id: str,
    status: str,
    *,
    evidence_ids: list[str] | None = None,
    actor: str = "user",
) -> ProjectParticipation:
    participation = session.get(ProjectParticipation, participation_id)
    if participation is None:
        raise LookupError(f"participation not found: {participation_id}")
    if status not in list(ParticipationStatus):
        raise ValueError(f"unknown participation status: {status!r}")
    if participation.status in (ParticipationStatus.HISTORICAL, ParticipationStatus.RETRACTED):
        raise ValueError(f"participation is already {participation.status}; history is immutable")
    evidence = validate_evidence_ids(session, evidence_ids)
    combined_evidence = sorted(set(participation.evidence_ids) | set(evidence))
    if status in (ParticipationStatus.PROBABLE, ParticipationStatus.CONFIRMED):
        if not combined_evidence:
            raise ValueError(f"{status} participation requires evidence")
        progression = {
            ParticipationStatus.UNCONFIRMED: 0,
            ParticipationStatus.PROBABLE: 1,
            ParticipationStatus.CONFIRMED: 2,
        }
        current_rank = progression.get(participation.status, 0)
        if progression[status] < current_rank:
            raise ValueError("participation confidence cannot be downgraded; retract it instead")
    participation.evidence_ids = combined_evidence
    participation.status = status
    participation.last_observed_at = utc_now()
    session.flush()
    audit.record(
        session,
        actor,
        "project.participation_status_changed",
        "project_participation",
        participation.id,
        {"status": status, "evidence_count": len(participation.evidence_ids)},
    )
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
