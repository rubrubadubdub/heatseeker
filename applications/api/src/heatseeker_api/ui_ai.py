"""Source Scout control panel and actions."""

import re
from datetime import timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from heatseeker_ai.models import (
    ActivationMode,
    ProposalStatus,
    ResearchPlan,
    ResearchRun,
    ScoutProvider,
    SourceProposal,
)
from heatseeker_ai.providers import all_provider_health
from heatseeker_ai.service import DEFAULT_BUDGETS, create_run
from heatseeker_common import audit, jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, JobStatus, PriorityClass
from heatseeker_common.timeutil import utc_now
from heatseeker_source_registry.models import ResearchScope, SourceDefinition, SourceLifecycle
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)


def _values(raw: str) -> list[str]:
    return sorted({value.strip() for value in re.split(r"[,;\n]", raw) if value.strip()})


@router.get("/source-scout", response_class=HTMLResponse)
def source_scout_page(request: Request):
    with session_scope(request.app.state.engine) as session:
        plans = list(session.scalars(select(ResearchPlan).order_by(ResearchPlan.name)))
        runs = list(
            session.scalars(
                select(ResearchRun)
                .options(selectinload(ResearchRun.plan))
                .order_by(ResearchRun.created_at.desc())
                .limit(25)
            )
        )
        proposals = list(
            session.scalars(
                select(SourceProposal)
                .where(SourceProposal.status == ProposalStatus.PROPOSED)
                .order_by(SourceProposal.confidence.desc(), SourceProposal.created_at.desc())
                .limit(100)
            )
        )
        scopes = list(session.scalars(select(ResearchScope).order_by(ResearchScope.name)))
        session.expunge_all()
    return _render(
        request,
        "source_scout.html",
        active="source_scout",
        plans=plans,
        runs=runs,
        proposals=proposals,
        scopes=scopes,
        provider_health=all_provider_health(request.app.state.settings),
        default_budgets=DEFAULT_BUDGETS,
    )


@router.post("/source-scout/plans")
def source_scout_create_plan(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    provider: str = Form(...),
    model: str = Form(default=""),
    scope_id: str = Form(default=""),
    keywords: str = Form(default=""),
    excluded_terms: str = Form(default=""),
    allowed_domains: str = Form(default=""),
    blocked_domains: str = Form(default=""),
    source_categories: str = Form(default=""),
    languages: str = Form(default=""),
    min_confidence: float = Form(default=0.65),
    instructions: str = Form(default=""),
    max_candidates: int = Form(default=25),
    max_turns: int = Form(default=8),
    max_budget_usd: float = Form(default=3.0),
    timeout_seconds: int = Form(default=900),
    activation_mode: str = Form(default=ActivationMode.PROPOSAL_ONLY),
    schedule_enabled: bool = Form(default=False),
    interval_minutes: int = Form(default=1440),
):
    name = name.strip()
    if not name or len(name) > 200:
        return _redirect("/source-scout", "Plan name must contain 1-200 characters", "danger")
    if provider not in {ScoutProvider.CODEX, ScoutProvider.CLAUDE, ScoutProvider.DISABLED}:
        return _redirect("/source-scout", "Unknown provider", "danger")
    if activation_mode not in {ActivationMode.PROPOSAL_ONLY, ActivationMode.AUTO_ACTIVATE}:
        return _redirect("/source-scout", "Unknown activation mode", "danger")
    if not 1 <= max_candidates <= 200 or not 1 <= max_turns <= 30:
        return _redirect("/source-scout", "Candidate/turn budgets are outside limits", "danger")
    if not 10 <= timeout_seconds <= 3600 or not 0.01 <= max_budget_usd <= 100:
        return _redirect("/source-scout", "Time or cost budget is outside limits", "danger")
    if schedule_enabled and interval_minutes < 5:
        return _redirect(
            "/source-scout", "Scheduled plans must be at least 5 minutes apart", "danger"
        )
    if not 0 <= min_confidence <= 1:
        return _redirect("/source-scout", "Minimum confidence must be between 0 and 1", "danger")
    search_config = {
        "keywords": _values(keywords),
        "excluded_terms": _values(excluded_terms),
        "allowed_domains": _values(allowed_domains),
        "blocked_domains": _values(blocked_domains),
        "source_categories": _values(source_categories),
        "languages": _values(languages),
        "min_confidence": min_confidence,
    }
    with session_scope(request.app.state.engine) as session:
        if session.scalars(select(ResearchPlan.id).where(ResearchPlan.name == name)).first():
            return _redirect("/source-scout", "A plan with that name already exists", "warning")
        if scope_id and session.get(ResearchScope, scope_id) is None:
            return _redirect("/source-scout", "Research scope not found", "danger")
        plan = ResearchPlan(
            name=name,
            description=description.strip() or None,
            provider=provider,
            model=model.strip() or None,
            scope_id=scope_id or None,
            search_config=search_config,
            instructions=instructions.strip(),
            budgets={
                "max_candidates": max_candidates,
                "max_turns": max_turns,
                "max_budget_usd": max_budget_usd,
                "timeout_seconds": timeout_seconds,
            },
            activation_mode=activation_mode,
            schedule_enabled=schedule_enabled,
            interval_minutes=interval_minutes if schedule_enabled else None,
            next_run_at=(
                utc_now() + timedelta(minutes=interval_minutes) if schedule_enabled else None
            ),
        )
        session.add(plan)
        session.flush()
        audit.record(
            session,
            "ui",
            "source_scout.plan_created",
            "research_plan",
            plan.id,
            {"provider": provider, "activation_mode": activation_mode},
        )
    return _redirect("/source-scout", f"Created source-scout plan {name}")


@router.post("/source-scout/plans/{plan_id}/run")
def source_scout_run_plan(request: Request, plan_id: str):
    with session_scope(request.app.state.engine) as session:
        plan = session.get(ResearchPlan, plan_id)
        if plan is None:
            return _redirect("/source-scout", "Plan not found", "danger")
        if not plan.is_enabled:
            return _redirect("/source-scout", "Plan is disabled", "warning")
        run = create_run(session, plan, trigger="manual", actor="ui")
        run_id = run.id
    return _redirect(f"/source-scout/runs/{run_id}", f"Source scout queued ({run_id[:8]})")


@router.post("/source-scout/plans/{plan_id}/toggle")
def source_scout_toggle_plan(request: Request, plan_id: str):
    with session_scope(request.app.state.engine) as session:
        plan = session.get(ResearchPlan, plan_id)
        if plan is None:
            return _redirect("/source-scout", "Plan not found", "danger")
        plan.is_enabled = not plan.is_enabled
        plan.updated_at = utc_now()
        audit.record(
            session,
            "ui",
            "source_scout.plan_toggled",
            "research_plan",
            plan.id,
            {"is_enabled": plan.is_enabled},
        )
        state = "enabled" if plan.is_enabled else "disabled"
    return _redirect("/source-scout", f"Plan {state}")


@router.get("/source-scout/runs/{run_id}", response_class=HTMLResponse)
def source_scout_run_detail(request: Request, run_id: str):
    with session_scope(request.app.state.engine) as session:
        run = session.scalars(
            select(ResearchRun)
            .where(ResearchRun.id == run_id)
            .options(
                selectinload(ResearchRun.plan),
                selectinload(ResearchRun.invocations),
                selectinload(ResearchRun.proposals),
            )
        ).first()
        if run is None:
            return _render(
                request,
                "error.html",
                active="source_scout",
                title="Scout run not found",
                detail=f"No source-scout run with id {run_id}",
            )
        job = session.get(Job, run.job_id) if run.job_id else None
        session.expunge_all()
    elapsed_seconds = None
    if run.status in ("queued", "running"):
        reference = run.started_at or run.created_at
        if reference is not None:
            elapsed_seconds = max(0, int((utc_now() - reference).total_seconds()))
    return _render(
        request,
        "source_scout_run.html",
        active="source_scout",
        run=run,
        job=job,
        elapsed_seconds=elapsed_seconds,
    )


@router.post("/source-scout/runs/{run_id}/cancel")
def source_scout_cancel_run(request: Request, run_id: str):
    with session_scope(request.app.state.engine) as session:
        run = session.get(ResearchRun, run_id)
        ok = bool(run and run.job_id and jobs.cancel(session, run.job_id, actor="ui"))
        job = session.get(Job, run.job_id) if run and run.job_id else None
        if ok and job and job.status == JobStatus.CANCELLED:
            run.status = "cancelled"
            run.error = "job cancelled before provider execution"
            run.finished_at = utc_now()
    if not ok:
        return _redirect(f"/source-scout/runs/{run_id}", "Run is not cancellable", "warning")
    return _redirect(f"/source-scout/runs/{run_id}", "Cancellation requested")


@router.post("/source-scout/proposals/{proposal_id}/activate")
def source_scout_activate(request: Request, proposal_id: str):
    with session_scope(request.app.state.engine) as session:
        proposal = session.get(SourceProposal, proposal_id)
        if proposal is None or proposal.status != ProposalStatus.PROPOSED:
            return _redirect("/source-scout", "Proposal is no longer actionable", "warning")
        job = jobs.enqueue(
            session,
            "source_scout.activate_proposal",
            payload={"proposal_id": proposal.id},
            priority=PriorityClass.INTERACTIVE,
            actor="ui",
        )
        job_id = job.id
    return _redirect("/source-scout", f"Policy check and activation queued ({job_id[:8]})")


@router.post("/source-scout/proposals/{proposal_id}/reject")
def source_scout_reject(request: Request, proposal_id: str, note: str = Form(default="")):
    with session_scope(request.app.state.engine) as session:
        proposal = session.get(SourceProposal, proposal_id)
        if proposal is None or proposal.status != ProposalStatus.PROPOSED:
            return _redirect("/source-scout", "Proposal is no longer actionable", "warning")
        proposal.status = ProposalStatus.REJECTED
        proposal.review_note = note.strip() or "rejected by user"
        proposal.reviewed_at = utc_now()
        source = session.get(SourceDefinition, proposal.source_definition_id)
        if source is not None and source.lifecycle_status == SourceLifecycle.PROPOSED:
            source.lifecycle_status = SourceLifecycle.REJECTED
            source.updated_at = utc_now()
        audit.record(
            session,
            "ui",
            "source_scout.proposal_rejected",
            "source_proposal",
            proposal.id,
            {"note": proposal.review_note},
        )
    return _redirect("/source-scout", "Proposal rejected")
