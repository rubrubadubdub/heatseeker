"""Server-rendered browser GUI (ADR-0009): Jinja2 + Bootstrap + htmx.

Pages call the same Python functions as the JSON API — never HTTP round-trips.
POST-redirect-GET with ?msg=&level= for action feedback; htmx polls table partials.
"""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from heatseeker_common import backup as backup_module
from heatseeker_common import jobs as jobs_module
from heatseeker_common.db import session_scope
from heatseeker_common.health import check_health
from heatseeker_common.models import Job, JobStatus, PriorityClass
from heatseeker_industry_packs.loader import (
    PackValidationError,
    default_packs_root,
    discover_packs,
    load_pack,
)
from heatseeker_industry_packs.models import PackRegistration
from heatseeker_industry_packs.registry import register_pack
from sqlalchemy import func, select

from heatseeker_api import guidance

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUS_BADGES = {
    "queued": "secondary",
    "running": "primary",
    "succeeded": "success",
    "failed": "danger",
    "cancelled": "warning",
}
templates.env.globals["status_badges"] = STATUS_BADGES
templates.env.globals["priority_classes"] = {int(p): p.name for p in PriorityClass}


def _render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("level", request.query_params.get("level", "info"))
    return templates.TemplateResponse(request, template, context)


def _redirect(url: str, msg: str, level: str = "success") -> RedirectResponse:
    return RedirectResponse(f"{url}?msg={quote(msg)}&level={level}", status_code=303)


def _pack_rows(session) -> list[dict]:
    rows = []
    for pack_path in discover_packs():
        try:
            pack = load_pack(pack_path)
            registered = session.get(PackRegistration, pack.pack_id)
            rows.append(
                {
                    "pack_id": pack.pack_id,
                    "name": pack.manifest.name,
                    "version": pack.version,
                    "valid": True,
                    "problems": [],
                    "registered": registered,
                    "up_to_date": bool(registered and registered.content_hash == pack.content_hash),
                }
            )
        except PackValidationError as exc:
            rows.append(
                {
                    "pack_id": pack_path.name,
                    "name": pack_path.name,
                    "version": None,
                    "valid": False,
                    "problems": exc.problems,
                    "registered": None,
                    "up_to_date": False,
                }
            )
    return rows


# --- Dashboard ---------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    engine = request.app.state.engine
    settings = request.app.state.settings
    health = check_health(engine, settings)
    with session_scope(engine) as session:
        counts = dict(
            session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
        )
        recent_jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(5)))
        packs = _pack_rows(session)
        steps = guidance.next_steps(session)
        last_advance = session.scalars(
            select(Job)
            .where(Job.job_type == "pipeline.advance")
            .order_by(Job.created_at.desc())
            .limit(1)
        ).first()
        session.expunge_all()
    backups = backup_module.list_backups(settings)
    return _render(
        request,
        "dashboard.html",
        active="dashboard",
        health=health,
        job_counts=counts,
        jobs_total=sum(counts.values()),
        recent_jobs=recent_jobs,
        packs=packs,
        backups=backups[-3:][::-1],
        steps=steps,
        primary_step=guidance.primary_step(steps),
        last_advance=last_advance,
    )


@router.post("/pipeline/advance")
def pipeline_advance_action(request: Request):
    """One-click 'do the next right thing' — chained deterministic steps as a job."""
    engine = request.app.state.engine
    with session_scope(engine) as session:
        pending = session.scalars(
            select(Job.id).where(
                Job.job_type == "pipeline.advance", Job.status.in_(["queued", "running"])
            )
        ).first()
        if pending is None:
            jobs_module.enqueue(
                session,
                "pipeline.advance",
                priority=PriorityClass.INTERACTIVE,
                actor="user",
            )
    if pending is not None:
        return _redirect("/", "A pipeline advance is already running", "info")
    return _redirect(
        "/",
        "Pipeline advancing: collection tick, document processing backlog, duplicate "
        "scan, and profile refresh — watch progress under Jobs",
    )


# --- Jobs --------------------------------------------------------------------


def _jobs_context(request: Request, status: str | None, limit: int) -> dict:
    engine = request.app.state.engine
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Job.status == status)
    with session_scope(engine) as session:
        jobs = list(session.scalars(stmt))
        counts = dict(
            session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
        )
        session.expunge_all()
    return {
        "jobs": jobs,
        "job_counts": counts,
        "status_filter": status or "",
        "limit": limit,
        "statuses": [s.value for s in JobStatus],
    }


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    return _render(request, "jobs.html", active="jobs", **_jobs_context(request, status, limit))


@router.get("/jobs/table", response_class=HTMLResponse)
def jobs_table_partial(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    return _render(request, "_jobs_table.html", **_jobs_context(request, status, limit))


@router.post("/jobs/enqueue")
def jobs_enqueue(
    request: Request,
    job_type: str = Form(...),
    payload: str = Form(default="{}"),
    priority: int = Form(default=int(PriorityClass.BACKGROUND_ENRICHMENT)),
):
    try:
        payload_data = json.loads(payload or "{}")
        if not isinstance(payload_data, dict):
            raise ValueError("payload must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return _redirect("/jobs", f"Invalid payload: {exc}", "danger")
    with session_scope(request.app.state.engine) as session:
        job = jobs_module.enqueue(
            session, job_type.strip(), payload=payload_data, priority=priority, actor="ui"
        )
        job_id = job.id
    return _redirect("/jobs", f"Enqueued {job_type} ({job_id[:8]})")


@router.post("/jobs/{job_id}/cancel")
def jobs_cancel(request: Request, job_id: str):
    with session_scope(request.app.state.engine) as session:
        ok = jobs_module.cancel(session, job_id, actor="ui")
    if ok:
        return _redirect("/jobs", f"Cancel requested for {job_id[:8]}")
    return _redirect("/jobs", f"Job {job_id[:8]} is not cancellable", "warning")


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str):
    with session_scope(request.app.state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            return _render(
                request,
                "error.html",
                active="jobs",
                title="Job not found",
                detail=f"No job with id {job_id}",
            )
        session.expunge(job)
    return _render(request, "job_detail.html", active="jobs", job=job)


# --- Packs -------------------------------------------------------------------


@router.get("/packs", response_class=HTMLResponse)
def packs_page(request: Request):
    with session_scope(request.app.state.engine) as session:
        packs = _pack_rows(session)
        session.expunge_all()
    return _render(request, "packs.html", active="packs", packs=packs)


@router.post("/packs/{pack_id}/load")
def packs_load(request: Request, pack_id: str):
    try:
        pack = load_pack(default_packs_root() / pack_id)
    except PackValidationError as exc:
        return _redirect("/packs", f"{pack_id} invalid: {len(exc.problems)} problem(s)", "danger")
    with session_scope(request.app.state.engine) as session:
        registration = register_pack(session, pack, actor="ui")
        version = registration.version
    return _redirect("/packs", f"Loaded {pack_id} v{version}")


@router.get("/packs/{pack_id}", response_class=HTMLResponse)
def pack_detail(request: Request, pack_id: str):
    problems: list[str] = []
    pack = None
    try:
        pack = load_pack(default_packs_root() / pack_id)
    except PackValidationError as exc:
        problems = exc.problems
    with session_scope(request.app.state.engine) as session:
        registered = session.get(PackRegistration, pack_id)
        if registered:
            session.expunge(registered)

    context: dict = {
        "active": "packs",
        "pack_id": pack_id,
        "problems": problems,
        "registered": registered,
        "pack": pack,
    }
    if pack is not None:
        files = pack.files
        get = files.get
        archetypes = get("company_archetypes.yaml")
        taxonomy = get("service_taxonomy.yaml")
        seeds = get("sources/seed_sources.yaml")
        context.update(
            manifest=pack.manifest,
            content_hash=pack.content_hash,
            archetypes=archetypes.archetypes if archetypes else [],
            categories=taxonomy.categories if taxonomy else [],
            segments=(get("market_segments.yaml").segments if get("market_segments.yaml") else []),
            systems=(get("products_systems.yaml").systems if get("products_systems.yaml") else []),
            event_types=(get("event_types.yaml").event_types if get("event_types.yaml") else []),
            terminology=get("terminology.yaml"),
            seed_sources=seeds.sources if seeds else [],
            discovery=seeds.discovery if seeds else None,
        )
    return _render(request, "pack_detail.html", **context)


# --- Backups -----------------------------------------------------------------


@router.get("/backups", response_class=HTMLResponse)
def backups_page(request: Request):
    settings = request.app.state.settings
    backups = backup_module.list_backups(settings)[::-1]
    return _render(
        request,
        "backups.html",
        active="backups",
        backups=backups,
        backups_dir=str(settings.backups_dir),
    )


@router.post("/backups/create")
def backups_create(request: Request):
    try:
        dest = backup_module.create_backup(request.app.state.settings)
    except FileNotFoundError as exc:
        return _redirect("/backups", str(exc), "danger")
    return _redirect("/backups", f"Backup created: {dest.name}")


# --- Health ------------------------------------------------------------------


@router.get("/health-ui", response_class=HTMLResponse)
def health_page(request: Request):
    report = check_health(request.app.state.engine, request.app.state.settings)
    return _render(request, "health.html", active="health", report=report)
