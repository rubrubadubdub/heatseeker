"""GUI routes for offerings, the lead queue, suppression, and XLSX export (M8)."""

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from heatseeker_common import jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, PriorityClass
from heatseeker_lead_intelligence import service
from heatseeker_lead_intelligence.export import build_lead_workbook
from heatseeker_lead_intelligence.models import Offering, SuppressionReason
from sqlalchemy import select

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)

STAGE_BADGES = {
    "identified": "primary",
    "researching": "info",
    "qualified": "success",
    "suppressed": "danger",
    "archived": "secondary",
}


def _split_ids(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()]


@router.get("/leads", response_class=HTMLResponse)
def leads_page(
    request: Request,
    offering_id: Annotated[str | None, Query()] = None,
    show_suppressed: Annotated[str | None, Query()] = None,
):
    with session_scope(request.app.state.engine) as session:
        offerings = service.list_offerings(session)
        selected = None
        queue = []
        last_rescore = None
        if offering_id:
            selected = session.get(Offering, offering_id)
        elif offerings:
            selected = offerings[0]
        if selected is not None:
            queue = service.lead_queue(
                session, selected.id, include_suppressed=show_suppressed == "1"
            )
            last_rescore = session.scalars(
                select(Job)
                .where(Job.job_type == "leads.rescore")
                .order_by(Job.created_at.desc())
                .limit(1)
            ).first()
        return _render(
            request,
            "leads.html",
            active="leads",
            offerings=offerings,
            selected=selected,
            queue=queue,
            show_suppressed=show_suppressed == "1",
            stage_badges=STAGE_BADGES,
            suppression_reasons=[r.value for r in SuppressionReason],
            last_rescore=last_rescore,
        )


@router.post("/leads/offerings")
def create_offering_action(
    request: Request,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    target_archetypes: Annotated[str, Form()] = "",
    target_capabilities: Annotated[str, Form()] = "",
    need_gap_capabilities: Annotated[str, Form()] = "",
    negative_archetypes: Annotated[str, Form()] = "",
    geo_codes: Annotated[str, Form()] = "",
):
    try:
        with session_scope(request.app.state.engine) as session:
            offering = service.create_offering(
                session,
                name,
                description=description,
                target_archetype_ids=_split_ids(target_archetypes),
                target_capability_ids=_split_ids(target_capabilities),
                need_gap_capability_ids=_split_ids(need_gap_capabilities),
                negative_archetype_ids=_split_ids(negative_archetypes),
                geo_codes=[code.upper() for code in _split_ids(geo_codes)],
            )
            offering_id = offering.id
            jobs.enqueue(
                session,
                "leads.rescore",
                payload={"offering_id": offering_id},
                priority=PriorityClass.INTERACTIVE,
                actor="user",
            )
    except ValueError as exc:
        return _redirect("/leads", str(exc), "danger")
    return _redirect(
        f"/leads?offering_id={offering_id}",
        "Offering created — first scoring run queued, refresh in a moment",
    )


@router.post("/leads/offerings/{offering_id}/rescore")
def rescore_action(request: Request, offering_id: str):
    with session_scope(request.app.state.engine) as session:
        if session.get(Offering, offering_id) is None:
            return _redirect("/leads", "Offering not found", "danger")
        pending = session.scalars(
            select(Job.id).where(
                Job.job_type == "leads.rescore",
                Job.status.in_(["queued", "running"]),
            )
        ).first()
        if pending is None:
            jobs.enqueue(
                session,
                "leads.rescore",
                payload={"offering_id": offering_id},
                priority=PriorityClass.INTERACTIVE,
                actor="user",
            )
    message = (
        "A rescore is already running" if pending else "Rescore queued — refresh shortly"
    )
    return _redirect(f"/leads?offering_id={offering_id}", message, "info")


@router.post("/leads/suppress")
def suppress_action(
    request: Request,
    organisation_id: Annotated[str, Form()],
    offering_id: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "other",
    note: Annotated[str, Form()] = "",
):
    if reason not in [r.value for r in SuppressionReason]:
        return _redirect("/leads", "Unknown suppression reason", "danger")
    with session_scope(request.app.state.engine) as session:
        service.suppress(
            session, organisation_id, reason=reason, note=note.strip() or None
        )
    target = f"/leads?offering_id={offering_id}" if offering_id else "/leads"
    return _redirect(target, "Suppressed — excluded from queue and every export")


@router.post("/leads/suppression/{rule_id}/lift")
def lift_suppression_action(
    request: Request, rule_id: str, offering_id: Annotated[str, Form()] = ""
):
    try:
        with session_scope(request.app.state.engine) as session:
            service.lift_suppression(session, rule_id)
    except LookupError as exc:
        return _redirect("/leads", str(exc), "danger")
    target = f"/leads?offering_id={offering_id}" if offering_id else "/leads"
    return _redirect(target, "Suppression lifted — rescore to restore the lead", "info")


@router.get("/leads/{offering_id}/export.xlsx")
def export_leads_xlsx(request: Request, offering_id: str):
    with session_scope(request.app.state.engine) as session:
        offering = session.get(Offering, offering_id)
        if offering is None:
            return _redirect("/leads", "Offering not found", "danger")
        payload = build_lead_workbook(session, offering)
        safe_name = "".join(
            ch if ch.isalnum() or ch in "-_ " else "_" for ch in offering.name
        ).strip().replace(" ", "_")
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="heatseeker_leads_{safe_name}.xlsx"'
        },
    )
