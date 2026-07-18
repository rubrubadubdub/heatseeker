"""JSON API for offerings, leads, suppression, and XLSX export (M8), under /api/*."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from heatseeker_common.db import session_scope
from heatseeker_lead_intelligence import service
from heatseeker_lead_intelligence.export import build_lead_workbook
from heatseeker_lead_intelligence.models import Offering, SuppressionReason
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api", tags=["leads"])


class OfferingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    description: str | None = None
    target_archetype_ids: list[str] = Field(default_factory=list)
    target_capability_ids: list[str] = Field(default_factory=list)
    need_gap_capability_ids: list[str] = Field(default_factory=list)
    negative_archetype_ids: list[str] = Field(default_factory=list)
    geo_codes: list[str] = Field(default_factory=list)
    scoring_weights: dict[str, float] = Field(default_factory=dict)


class SuppressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: SuppressionReason = SuppressionReason.OTHER
    note: str | None = None


def _lead_dict(lead) -> dict:
    return {
        "organisation_id": lead.organisation_id,
        "organisation_name": lead.organisation.canonical_name,
        "offering_id": lead.offering_id,
        "commercial_priority": lead.commercial_priority,
        "fit_score": lead.fit_score,
        "timing_score": lead.timing_score,
        "evidence_quality_score": lead.evidence_quality_score,
        "accessibility_score": lead.accessibility_score,
        "relationship_score": lead.relationship_score,
        "opportunity_stage": lead.opportunity_stage,
        "component_scores": lead.component_scores,
        "reasons": lead.reasons,
        "risks": lead.risks,
        "unknowns": lead.unknowns,
        "next_action": lead.next_action,
        "rule_version": lead.rule_version,
        "scored_at": lead.scored_at.isoformat(),
    }


@router.get("/offerings")
def api_list_offerings(request: Request):
    with session_scope(request.app.state.engine) as session:
        return {
            "offerings": [
                {
                    "id": offering.id,
                    "name": offering.name,
                    "status": offering.status,
                    "target_archetype_ids": offering.target_archetype_ids,
                    "target_capability_ids": offering.target_capability_ids,
                    "need_gap_capability_ids": offering.need_gap_capability_ids,
                    "geo_codes": offering.geo_codes,
                }
                for offering in service.list_offerings(session)
            ]
        }


@router.post("/offerings", status_code=201)
def api_create_offering(request: Request, payload: OfferingCreateRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            offering = service.create_offering(
                session,
                payload.name,
                description=payload.description,
                target_archetype_ids=payload.target_archetype_ids,
                target_capability_ids=payload.target_capability_ids,
                need_gap_capability_ids=payload.need_gap_capability_ids,
                negative_archetype_ids=payload.negative_archetype_ids,
                geo_codes=[code.upper() for code in payload.geo_codes],
                scoring_weights=payload.scoring_weights,
                actor="api",
            )
            return {"id": offering.id, "name": offering.name}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/offerings/{offering_id}/rescore")
def api_rescore(request: Request, offering_id: str):
    try:
        with session_scope(request.app.state.engine) as session:
            return service.rescore_offering(session, offering_id, actor="api")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/leads")
def api_leads(
    request: Request,
    offering_id: Annotated[str, Query()],
    include_suppressed: bool = False,
):
    with session_scope(request.app.state.engine) as session:
        if session.get(Offering, offering_id) is None:
            raise HTTPException(status_code=404, detail="offering not found")
        return {
            "leads": [
                _lead_dict(lead)
                for lead in service.lead_queue(
                    session, offering_id, include_suppressed=include_suppressed
                )
            ]
        }


@router.get("/leads/export.xlsx")
def api_export_leads(request: Request, offering_id: Annotated[str, Query()]):
    with session_scope(request.app.state.engine) as session:
        offering = session.get(Offering, offering_id)
        if offering is None:
            raise HTTPException(status_code=404, detail="offering not found")
        payload = build_lead_workbook(session, offering)
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="heatseeker_leads.xlsx"'},
    )


@router.post("/organisations/{organisation_id}/suppress")
def api_suppress(request: Request, organisation_id: str, payload: SuppressRequest):
    try:
        with session_scope(request.app.state.engine) as session:
            rule = service.suppress(
                session,
                organisation_id,
                reason=payload.reason.value,
                note=payload.note,
                actor="api",
            )
            return {"rule_id": rule.id, "reason": rule.reason, "active": rule.active}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/suppressions/{rule_id}/lift")
def api_lift_suppression(request: Request, rule_id: str):
    try:
        with session_scope(request.app.state.engine) as session:
            rule = service.lift_suppression(session, rule_id, actor="api")
            return {"rule_id": rule.id, "active": rule.active}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
