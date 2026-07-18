"""JSON API for projects and the knowledge graph (M6), under /api/*."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_knowledge_graph import graph, projects
from heatseeker_knowledge_graph.models import RELATIONSHIP_TYPES
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api", tags=["graph"])


def _edge_dict(edge: graph.Edge) -> dict:
    return {
        "kind": edge.kind,
        "label": edge.label,
        "direction": edge.direction,
        "other_id": edge.other_id,
        "confidence": edge.confidence,
        "evidence_count": edge.evidence_count,
        "ref_id": edge.ref_id,
        "valid_from": edge.valid_from.isoformat() if edge.valid_from else None,
        "valid_to": edge.valid_to.isoformat() if edge.valid_to else None,
        "detail": edge.detail,
    }


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=500)
    status: str = "unknown"
    project_type_ids: list[str] = Field(default_factory=list)
    sector_ids: list[str] = Field(default_factory=list)
    description: str | None = None


class ParticipationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organisation_id: str
    role_type: str = Field(min_length=1, max_length=50)
    status: str = "unconfirmed"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)


class RelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_entity_id: str
    object_entity_id: str
    relationship_type: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)


@router.get("/projects")
def api_list_projects(request: Request, q: Annotated[str | None, Query()] = None):
    with session_scope(request.app.state.engine) as session:
        return {
            "projects": [
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status,
                    "project_type_ids": project.project_type_ids,
                    "participant_count": len(project.participations),
                }
                for project in projects.list_projects(session, query=q)
            ]
        }


@router.post("/projects", status_code=201)
def api_create_project(request: Request, payload: ProjectCreateRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            project = projects.create_project(
                session,
                payload.name,
                status=payload.status,
                project_type_ids=payload.project_type_ids,
                sector_ids=payload.sector_ids,
                description=payload.description,
            )
            return {"id": project.id, "name": project.name, "status": project.status}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects/{project_id}")
def api_project_detail(request: Request, project_id: str):
    with session_scope(request.app.state.engine) as session:
        project = projects.get_project(session, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "id": project.id,
            "name": project.name,
            "status": project.status,
            "project_type_ids": project.project_type_ids,
            "sector_ids": project.sector_ids,
            "description": project.description,
            "participants": [
                {
                    "organisation_id": p.organisation_id,
                    "organisation_name": p.organisation.canonical_name,
                    "role_type": p.role_type,
                    "status": p.status,
                    "confidence": p.confidence,
                    "evidence_count": len(p.evidence_ids),
                }
                for p in project.participations
            ],
        }


@router.post("/projects/{project_id}/participants")
def api_add_participant(request: Request, project_id: str, payload: ParticipationRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            participation = projects.add_participation(
                session,
                project_id,
                payload.organisation_id,
                payload.role_type,
                status=payload.status,
                confidence=payload.confidence,
                evidence_ids=payload.evidence_ids,
            )
            return {
                "id": participation.id,
                "organisation_id": participation.organisation_id,
                "role_type": participation.role_type,
                "status": participation.status,
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/relationships/types")
def api_relationship_types():
    return {"types": list(RELATIONSHIP_TYPES)}


@router.post("/relationships", status_code=201)
def api_create_relationship(request: Request, payload: RelationshipRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            edge = graph.add_relationship(
                session,
                payload.subject_entity_id,
                payload.object_entity_id,
                payload.relationship_type,
                confidence=payload.confidence,
                evidence_ids=payload.evidence_ids,
                created_by="api",
            )
            return {
                "id": edge.id,
                "subject_entity_id": edge.subject_entity_id,
                "object_entity_id": edge.object_entity_id,
                "relationship_type": edge.relationship_type,
                "status": edge.status,
                "confidence": edge.confidence,
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except graph.GraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/relationships/{relationship_id}/end")
def api_end_relationship(request: Request, relationship_id: str):
    try:
        with session_scope(request.app.state.engine) as session:
            edge = graph.end_relationship(session, relationship_id)
            return {
                "id": edge.id,
                "status": edge.status,
                "valid_to": edge.valid_to.isoformat(),
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except graph.GraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/graph/{entity_id}/neighbourhood")
def api_neighbourhood(
    request: Request,
    entity_id: str,
    depth: Annotated[int, Query(ge=1, le=4)] = 2,
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = 0.0,
):
    with session_scope(request.app.state.engine) as session:
        if entities.get_organisation(session, entity_id) is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        neighbours = graph.neighbourhood(
            session, entity_id, depth=depth, min_confidence=min_confidence
        )
        return {
            "neighbours": [
                {
                    "organisation_id": n.organisation.id,
                    "organisation_name": n.organisation.canonical_name,
                    "hops": n.hops,
                    "path_confidence": n.best_confidence,
                    "via": [
                        {"edge": _edge_dict(hop.edge), "node_id": hop.node_id}
                        for hop in n.via
                    ],
                }
                for n in neighbours
            ]
        }


@router.get("/graph/paths")
def api_paths(
    request: Request,
    from_id: Annotated[str, Query(alias="from")],
    to_id: Annotated[str, Query(alias="to")],
    max_depth: Annotated[int, Query(ge=1, le=4)] = 4,
):
    with session_scope(request.app.state.engine) as session:
        for organisation_id in (from_id, to_id):
            if entities.get_organisation(session, organisation_id) is None:
                raise HTTPException(status_code=404, detail="organisation not found")
        paths = graph.find_paths(session, from_id, to_id, max_depth=max_depth)
        return {
            "paths": [
                {
                    "confidence": graph.path_confidence(path),
                    "hops": [
                        {"edge": _edge_dict(hop.edge), "node_id": hop.node_id}
                        for hop in path
                    ],
                }
                for path in paths
            ]
        }
