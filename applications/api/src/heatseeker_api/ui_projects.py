"""GUI routes for projects and the knowledge graph (M6)."""

from datetime import UTC, date, datetime, time
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_knowledge_graph import graph, projects
from heatseeker_knowledge_graph.models import (
    PARTICIPATION_ROLES,
    RELATIONSHIP_TYPES,
    ParticipationStatus,
    ProjectStatus,
)

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)

PARTICIPATION_BADGES = {
    "confirmed": "success",
    "probable": "primary",
    "unconfirmed": "secondary",
    "historical": "secondary",
    "retracted": "danger",
}


def _split_ids(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]


def _start_of_day(value: date | None) -> datetime | None:
    return datetime.combine(value, time.min, tzinfo=UTC) if value else None


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, q: Annotated[str | None, Query(max_length=200)] = None):
    with session_scope(request.app.state.engine) as session:
        rows = projects.list_projects(session, query=q)
        return _render(
            request,
            "projects.html",
            active="projects",
            projects=rows,
            q=q or "",
            statuses=[s.value for s in ProjectStatus],
        )


@router.post("/projects/create")
def create_project_action(
    request: Request,
    name: Annotated[str, Form()],
    status: Annotated[str, Form()] = "unknown",
    project_types: Annotated[str, Form()] = "",
    sectors: Annotated[str, Form()] = "",
    estimated_value: Annotated[float | None, Form()] = None,
    currency: Annotated[str, Form()] = "",
    expected_start_date: Annotated[date | None, Form()] = None,
    expected_end_date: Annotated[date | None, Form()] = None,
    description: Annotated[str, Form()] = "",
    evidence_ids: Annotated[str, Form()] = "",
):
    if status not in [s.value for s in ProjectStatus]:
        return _redirect("/projects", "Invalid project status", "danger")
    try:
        with session_scope(request.app.state.engine) as session:
            project = projects.create_project(
                session,
                name,
                status=status,
                project_type_ids=_split_ids(project_types),
                sector_ids=_split_ids(sectors),
                estimated_value=estimated_value,
                currency=currency,
                expected_start_date=_start_of_day(expected_start_date),
                expected_end_date=_start_of_day(expected_end_date),
                description=description,
                evidence_ids=_split_ids(evidence_ids),
            )
            project_id = project.id
    except ValueError as exc:
        return _redirect("/projects", str(exc), "danger")
    return _redirect(f"/projects/{project_id}", "Project created — now add participants")


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str):
    with session_scope(request.app.state.engine) as session:
        project = projects.get_project(session, project_id)
        if project is None:
            return _redirect("/projects", "Project not found", "danger")
        organisations = entities.list_organisations(session, limit=200)
        return _render(
            request,
            "project_detail.html",
            active="projects",
            project=project,
            organisations=organisations,
            roles=PARTICIPATION_ROLES,
            participation_statuses=[s.value for s in ParticipationStatus],
            participation_badges=PARTICIPATION_BADGES,
        )


@router.post("/projects/{project_id}/participants")
def add_participant_action(
    request: Request,
    project_id: str,
    organisation_id: Annotated[str, Form()],
    role_type: Annotated[str, Form()],
    status: Annotated[str, Form()] = "unconfirmed",
    confidence: Annotated[float, Form()] = 0.5,
    contract_value: Annotated[float | None, Form()] = None,
    evidence_ids: Annotated[str, Form()] = "",
):
    try:
        with session_scope(request.app.state.engine) as session:
            projects.add_participation(
                session,
                project_id,
                organisation_id,
                role_type,
                status=status,
                confidence=confidence,
                contract_value=contract_value,
                evidence_ids=_split_ids(evidence_ids),
            )
    except (ValueError, LookupError) as exc:
        return _redirect(f"/projects/{project_id}", str(exc), "danger")
    return _redirect(f"/projects/{project_id}", "Participant recorded")


@router.post("/participations/{participation_id}/status")
def set_participation_status_action(
    request: Request,
    participation_id: str,
    status: Annotated[str, Form()],
    evidence_ids: Annotated[str, Form()] = "",
):
    try:
        with session_scope(request.app.state.engine) as session:
            participation = projects.set_participation_status(
                session,
                participation_id,
                status,
                evidence_ids=_split_ids(evidence_ids),
            )
            project_id = participation.project_id
    except (ValueError, LookupError) as exc:
        return _redirect("/projects", str(exc), "danger")
    return _redirect(f"/projects/{project_id}", f"Participation marked {status}")


@router.post("/relationships/create")
def create_relationship_action(
    request: Request,
    subject_entity_id: Annotated[str, Form()],
    object_entity_id: Annotated[str, Form()],
    relationship_type: Annotated[str, Form()],
    confidence: Annotated[float, Form()] = 0.5,
    evidence_ids: Annotated[str, Form()] = "",
):
    if relationship_type not in RELATIONSHIP_TYPES:
        return _redirect(
            f"/entities/{subject_entity_id}", "Unknown relationship type", "danger"
        )
    try:
        with session_scope(request.app.state.engine) as session:
            graph.add_relationship(
                session,
                subject_entity_id,
                object_entity_id,
                relationship_type,
                confidence=confidence,
                evidence_ids=_split_ids(evidence_ids),
                created_by="user",
            )
    except (graph.GraphError, LookupError, ValueError) as exc:
        return _redirect(f"/entities/{subject_entity_id}", str(exc), "danger")
    return _redirect(f"/entities/{subject_entity_id}", "Relationship recorded")


@router.post("/relationships/{relationship_id}/{action}")
def close_relationship_action(
    request: Request,
    relationship_id: str,
    action: str,
    entity_id: Annotated[str, Form()] = "",
):
    if action not in ("end", "retract"):
        return _redirect("/entities", "Unknown relationship action", "danger")
    try:
        with session_scope(request.app.state.engine) as session:
            if action == "end":
                graph.end_relationship(session, relationship_id)
            else:
                graph.retract_relationship(session, relationship_id)
    except (graph.GraphError, LookupError) as exc:
        return _redirect(f"/entities/{entity_id}" if entity_id else "/entities", str(exc), "danger")
    target = f"/entities/{entity_id}" if entity_id else "/entities"
    verb = "ended (dates retained)" if action == "end" else "retracted (kept for audit)"
    return _redirect(target, f"Relationship {verb}")
