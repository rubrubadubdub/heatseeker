"""JSON API for the entity core & resolution (M4), under /api/*."""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.matching import scan_for_duplicates
from heatseeker_entity_resolution.models import (
    EntityMatchCandidate,
    IdentifierScheme,
    Organisation,
    OrganisationType,
)
from heatseeker_entity_resolution.resolution import (
    ResolutionError,
    group_profile,
    list_queue,
    perform_merge,
    record_decision,
    reverse_merge,
)
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api", tags=["entities"])


def _org_summary(organisation: Organisation) -> dict:
    return {
        "id": organisation.id,
        "canonical_name": organisation.canonical_name,
        "legal_name": organisation.legal_name,
        "organisation_type": organisation.organisation_type,
        "status": organisation.status,
        "merged_into_id": organisation.merged_into_id,
        "identifiers": [
            {"scheme": i.scheme, "value": i.value, "is_current": i.is_current}
            for i in organisation.identifiers
        ],
        "domains": [d.domain for d in organisation.domains],
        "profile_completeness": organisation.profile_completeness,
        "provenance": organisation.provenance,
    }


def _candidate_summary(candidate: EntityMatchCandidate) -> dict:
    return {
        "id": candidate.id,
        "organisation_a_id": candidate.organisation_a_id,
        "organisation_b_id": candidate.organisation_b_id,
        "match_state": candidate.match_state,
        "score": candidate.score,
        "priority_score": candidate.priority_score,
        "commercial_importance": candidate.commercial_importance,
        "downstream_impact": candidate.downstream_impact,
        "ease_of_resolution": candidate.ease_of_resolution,
        "signals": candidate.signals,
        "conflict_count": candidate.conflict_count,
        "resolution": candidate.resolution,
        "resolved_by": candidate.resolved_by,
        "notes": candidate.notes,
    }


class EntityCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=500)
    legal_name: str | None = Field(default=None, max_length=500)
    trading_names: list[str] = Field(default_factory=list)
    organisation_type: OrganisationType = OrganisationType.UNKNOWN
    country_of_registration: str | None = Field(default=None, max_length=100)
    description: str | None = None
    identifiers: list[tuple[IdentifierScheme, str]] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    absorbed_id: str
    rationale: str = Field(min_length=1)


class ReverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["merge", "related", "distinct"]
    survivor_id: str | None = None
    notes: str | None = None


@router.get("/entities")
def api_list_entities(
    request: Request,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_merged: bool = False,
):
    with session_scope(request.app.state.engine) as session:
        rows = entities.list_organisations(session, query=q, include_merged=include_merged)
        return {"organisations": [_org_summary(o) for o in rows]}


@router.post("/entities", status_code=201)
def api_create_entity(request: Request, payload: EntityCreateRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            organisation = entities.create_organisation(
                session,
                payload.canonical_name,
                legal_name=payload.legal_name,
                trading_names=payload.trading_names,
                organisation_type=payload.organisation_type,
                country_of_registration=payload.country_of_registration,
                description=payload.description,
                identifiers=[(s.value, v) for s, v in payload.identifiers],
                domains=payload.domains,
            )
            return _org_summary(organisation)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/entities/{organisation_id}")
def api_entity_profile(request: Request, organisation_id: str):
    with session_scope(request.app.state.engine) as session:
        if entities.get_organisation(session, organisation_id) is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        profile = group_profile(session, organisation_id)
        return {
            "canonical": _org_summary(profile["canonical"]),
            "group": [_org_summary(o) for o in profile["group"]],
            "identifiers": [
                {"origin_id": row["origin"].id, "scheme": row["item"].scheme,
                 "value": row["item"].value}
                for row in profile["identifiers"]
            ],
            "domains": [
                {"origin_id": row["origin"].id, "domain": row["item"].domain}
                for row in profile["domains"]
            ],
            "contact_points": [
                {"origin_id": row["origin"].id, "contact_type": row["item"].contact_type,
                 "value": row["item"].value}
                for row in profile["contact_points"]
            ],
            "units": [
                {"origin_id": row["origin"].id, "unit_type": row["item"].unit_type,
                 "name": row["item"].name}
                for row in profile["units"]
            ],
        }


@router.post("/entities/{organisation_id}/merge")
def api_merge(request: Request, organisation_id: str, payload: MergeRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            merge = perform_merge(
                session,
                organisation_id,
                payload.absorbed_id,
                rationale=payload.rationale,
                performed_by="api",
            )
            return {
                "merge_id": merge.id,
                "survivor_id": merge.survivor_id,
                "absorbed_id": merge.absorbed_id,
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/merges/{merge_id}/reverse")
def api_reverse_merge(request: Request, merge_id: str, payload: ReverseRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            merge = reverse_merge(
                session, merge_id, reason=payload.reason, performed_by="api"
            )
            return {
                "merge_id": merge.id,
                "reversed_at": merge.reversed_at.isoformat(),
                "reversed_by": merge.reversed_by,
            }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/resolution/queue")
def api_resolution_queue(request: Request, include_resolved: bool = False):
    with session_scope(request.app.state.engine) as session:
        return {
            "candidates": [
                _candidate_summary(c)
                for c in list_queue(session, include_resolved=include_resolved)
            ]
        }


@router.post("/resolution/scan")
def api_resolution_scan(request: Request):
    with session_scope(request.app.state.engine) as session:
        return scan_for_duplicates(session)


@router.post("/resolution/{candidate_id}/decide")
def api_decide(request: Request, candidate_id: str, payload: DecisionRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            candidate = record_decision(
                session,
                candidate_id,
                payload.decision,
                resolved_by="api",
                notes=payload.notes,
                survivor_id=payload.survivor_id,
            )
            return _candidate_summary(candidate)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/companies/{organisation_id}/profile")
def api_company_profile(request: Request, organisation_id: str):
    """Full M5 profile: every field with confidence components and evidence links."""
    from heatseeker_intelligence import profile as intelligence_profile

    with session_scope(request.app.state.engine) as session:
        if entities.get_organisation(session, organisation_id) is None:
            raise HTTPException(status_code=404, detail="organisation not found")
        assembled = intelligence_profile.assemble(session, organisation_id)
        return {
            "canonical": _org_summary(assembled["identity"]["canonical"]),
            "duplicate_warning_count": len(assembled["duplicate_warnings"]),
            "facts": [
                {
                    "predicate": fact["predicate"],
                    "value": fact["value"],
                    "status": fact["status"],
                    "confidence": fact["confidence"],
                    "confidence_vocabulary": fact["confidence_vocabulary"],
                    "components": fact["components"],
                    "supporting_count": fact["supporting_count"],
                    "contradicting_count": fact["contradicting_count"],
                    "independent_source_count": fact["independent_source_count"],
                    "best_evidence_document_id": (
                        fact["best_evidence_document"].id
                        if fact["best_evidence_document"]
                        else None
                    ),
                    "last_observed_at": (
                        fact["last_observed_at"].isoformat()
                        if fact["last_observed_at"]
                        else None
                    ),
                }
                for fact in assembled["facts"]
            ],
            "classifications": [
                {
                    "pack_id": a.pack_id,
                    "taxonomy_id": a.taxonomy_id,
                    "category_id": a.category_id,
                    "category_label": a.category_label,
                    "assignment_type": a.assignment_type,
                    "confidence": a.confidence,
                    "evidence_count": len(a.evidence_ids),
                }
                for a in assembled["classifications"]
            ],
            "capabilities": [
                {
                    "capability_id": c.capability_id,
                    "capability_label": c.capability_label,
                    "capability_status": c.capability_status,
                    "evidence_strength": c.evidence_strength,
                    "evidence_count": len(c.evidence_ids),
                }
                for c in assembled["capabilities"]
            ],
            "size_estimates": [
                {
                    "concept": e.concept,
                    "band": e.band,
                    "confidence": e.confidence,
                    "basis": e.basis,
                }
                for e in assembled["size_estimates"]
            ],
            "research_questions": [
                {
                    "id": q.id,
                    "question_type": q.question_type,
                    "question_text": q.question_text,
                    "priority": q.priority,
                    "reason": q.reason,
                }
                for q in assembled["research_questions"]
            ],
        }


@router.get("/discovery/runs")
def api_discovery_runs(request: Request):
    from heatseeker_intelligence.discovery import list_runs

    with session_scope(request.app.state.engine) as session:
        return {
            "runs": [
                {
                    "id": run.id,
                    "dataset_name": run.dataset_name,
                    "publisher": run.publisher,
                    "status": run.status,
                    "checksum": run.checksum,
                    "row_count": run.row_count,
                    "imported_count": run.imported_count,
                    "matched_existing_count": run.matched_existing_count,
                    "skipped_out_of_scope_count": run.skipped_out_of_scope_count,
                    "rejected_count": run.rejected_count,
                    "created_at": run.created_at.isoformat(),
                }
                for run in list_runs(session)
            ]
        }
