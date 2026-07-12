"""Source-scout orchestration and deterministic proposal ingestion."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

from heatseeker_common import audit, jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, JobStatus, PriorityClass
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_source_registry.identity import attach_identity, resolve_identities, url_identity
from heatseeker_source_registry.models import ResearchScope, SourceDefinition, SourceLifecycle
from heatseeker_source_registry.policy import activation_blockers, check_robots, robots_enforced
from heatseeker_source_registry.targeting import CoverageSpec, TargetSpec, upsert_coverage
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from heatseeker_ai.contracts import CandidateSource, SourceExpansionRequest, SourceExpansionResult
from heatseeker_ai.models import (
    ActivationMode,
    AIInvocation,
    ProposalStatus,
    ResearchPlan,
    ResearchRun,
    ScoutRunStatus,
    SourceProposal,
)
from heatseeker_ai.prompts import PROMPT_VERSION, build_source_expansion_prompt
from heatseeker_ai.providers import ProviderError, ScoutCancelled, SourceScoutProvider, get_provider

DEFAULT_BUDGETS = {
    "max_candidates": 25,
    "max_turns": 8,
    "max_budget_usd": 3.0,
    "timeout_seconds": 900,
}


def _scope_snapshot(scope: ResearchScope | None) -> dict | None:
    if scope is None:
        return None
    return {
        "id": scope.id,
        "name": scope.name,
        "description": scope.description,
        "geo_codes": list(scope.geo_codes or []),
        "exclude_codes": list(scope.exclude_codes or []),
        "industry_ids": list(scope.industry_ids or []),
        "target_filters": dict(scope.target_filters or {}),
        "include_unknown": scope.include_unknown,
    }


def _plan_snapshot(plan: ResearchPlan) -> dict:
    return {
        "id": plan.id,
        "name": plan.name,
        "description": plan.description,
        "provider": plan.provider,
        "model": plan.model,
        "search_config": dict(plan.search_config or {}),
        "instructions": plan.instructions,
        "budgets": {**DEFAULT_BUDGETS, **dict(plan.budgets or {})},
        "activation_mode": plan.activation_mode,
    }


def create_run(
    session: Session,
    plan: ResearchPlan,
    *,
    trigger: str,
    actor: str,
) -> ResearchRun:
    scope = session.get(ResearchScope, plan.scope_id) if plan.scope_id else None
    run = ResearchRun(
        plan_id=plan.id,
        status=ScoutRunStatus.QUEUED,
        trigger=trigger,
        provider=plan.provider,
        model=plan.model,
        plan_snapshot=_plan_snapshot(plan),
        scope_snapshot=_scope_snapshot(scope),
        counters={},
    )
    session.add(run)
    session.flush()
    job = jobs.enqueue(
        session,
        "source_scout.run",
        payload={"run_id": run.id},
        priority=(
            PriorityClass.INTERACTIVE
            if trigger == "manual"
            else PriorityClass.LOW_PRIORITY_DISCOVERY
        ),
        correlation_id=run.id,
        actor=actor,
    )
    run.job_id = job.id
    if plan.schedule_enabled and plan.interval_minutes:
        plan.next_run_at = utc_now() + timedelta(minutes=plan.interval_minutes)
        plan.updated_at = utc_now()
    audit.record(session, actor, "source_scout.run_created", "research_run", run.id)
    return run


def enqueue_due_plans(session: Session) -> int:
    reconcile_terminal_runs(session)
    now = utc_now()
    plans = list(
        session.scalars(
            select(ResearchPlan).where(
                ResearchPlan.is_enabled.is_(True),
                ResearchPlan.schedule_enabled.is_(True),
                ResearchPlan.next_run_at.is_not(None),
                ResearchPlan.next_run_at <= now,
            )
        )
    )
    enqueued = 0
    for plan in plans:
        pending = session.scalars(
            select(ResearchRun.id)
            .join(Job, Job.id == ResearchRun.job_id)
            .where(
                ResearchRun.plan_id == plan.id,
                ResearchRun.status.in_([ScoutRunStatus.QUEUED, ScoutRunStatus.RUNNING]),
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        ).first()
        if pending:
            plan.next_run_at = now + timedelta(minutes=plan.interval_minutes or 5)
            plan.updated_at = now
            continue
        create_run(session, plan, trigger="schedule", actor="source-scout-scheduler")
        enqueued += 1
    return enqueued


def reconcile_terminal_runs(session: Session) -> int:
    """Mirror terminal job outcomes when a scout handler never got to update its run."""
    rows = session.execute(
        select(ResearchRun, Job)
        .join(Job, Job.id == ResearchRun.job_id)
        .where(
            ResearchRun.status.in_([ScoutRunStatus.QUEUED, ScoutRunStatus.RUNNING]),
            Job.status.in_([JobStatus.CANCELLED, JobStatus.FAILED]),
        )
    ).all()
    for run, job in rows:
        run.status = (
            ScoutRunStatus.CANCELLED if job.status == JobStatus.CANCELLED else ScoutRunStatus.FAILED
        )
        run.error = job.error or ("job cancelled before provider execution")
        run.finished_at = job.finished_at or utc_now()
    return len(rows)


def _existing_domains(session: Session) -> list[str]:
    domains = set()
    for url in session.scalars(select(SourceDefinition.base_url).where(SourceDefinition.base_url)):
        hostname = urlsplit(url).hostname
        if hostname:
            domains.add(hostname.lower())
    return sorted(domains)[:10_000]


def _request_for_run(session: Session, run: ResearchRun) -> SourceExpansionRequest:
    snapshot = run.plan_snapshot
    return SourceExpansionRequest(
        scope=run.scope_snapshot,
        search_config=snapshot.get("search_config") or {},
        instructions=snapshot.get("instructions") or "",
        budgets=snapshot.get("budgets") or {},
        existing_domains=_existing_domains(session),
    )


def _cancel_requested(engine: Engine, run_id: str) -> bool:
    with session_scope(engine) as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            return True
        job = session.get(Job, run.job_id) if run.job_id else None
        return bool(job and job.cancel_requested)


def _scope_targets(snapshot: dict | None) -> tuple[TargetSpec, ...]:
    if not snapshot:
        return ()
    targets: list[TargetSpec] = []
    targets.extend(TargetSpec("industry", value) for value in snapshot.get("industry_ids", []))
    targets.extend(TargetSpec("region", value) for value in snapshot.get("geo_codes", []))
    targets.extend(
        TargetSpec("region", value, polarity="exclude")
        for value in snapshot.get("exclude_codes", [])
    )
    for dimension, values in (snapshot.get("target_filters") or {}).items():
        targets.extend(TargetSpec(dimension, value) for value in values)
    return tuple(targets)


def _create_proposal(
    session: Session,
    run: ResearchRun,
    candidate: CandidateSource,
) -> SourceProposal:
    identity = url_identity(str(candidate.url))
    proposal = SourceProposal(
        run_id=run.id,
        url=str(candidate.url),
        normalised_url=identity.normalised_value,
        name=candidate.name,
        source_category=candidate.source_category,
        access_method=candidate.access_method,
        suggested_authority_tier=candidate.authority_tier,
        reasoning=candidate.reasoning,
        confidence=candidate.confidence,
        originating_query=candidate.originating_query,
        supporting_urls=[str(url) for url in candidate.supporting_urls],
        suggested_coverage=candidate.suggested_coverage,
    )
    session.add(proposal)
    session.flush()
    existing = resolve_identities(session, [identity])
    if existing is not None:
        proposal.source_definition_id = existing.id
        proposal.status = ProposalStatus.DUPLICATE
        return proposal
    source = SourceDefinition(
        name=candidate.name,
        source_category=candidate.source_category,
        base_url=str(candidate.url),
        access_method=candidate.access_method,
        authority_tier=6,
        lifecycle_status=SourceLifecycle.PROPOSED,
        origin="proposal",
        notes=f"AI source-scout proposal {proposal.id}: {candidate.reasoning[:1000]}",
    )
    session.add(source)
    session.flush()
    attach_identity(session, source, identity, origin="ai_scout", is_primary=True)
    proposal.source_definition_id = source.id
    targets = _scope_targets(run.scope_snapshot)
    if targets:
        upsert_coverage(
            session,
            source,
            CoverageSpec(
                coverage_key=f"scout-{run.id[:12]}",
                name=f"Scout run: {(run.scope_snapshot or {}).get('name', run.id[:8])}",
                description="Coverage proposed from the immutable source-scout scope snapshot.",
                targets=targets,
                confidence=candidate.confidence,
                origin="ai_scout",
            ),
            actor="source-scout",
        )
    audit.record(
        session,
        "source-scout",
        "source.proposed",
        "source",
        source.id,
        {"run_id": run.id, "proposal_id": proposal.id, "url": proposal.url},
    )
    return proposal


def _domain_matches(hostname: str, rule: str) -> bool:
    candidate = rule.strip().lower()
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    rule_host = parsed.hostname or candidate.split("/", 1)[0]
    return hostname == rule_host or hostname.endswith(f".{rule_host}")


def _candidate_allowed(candidate: CandidateSource, search_config: dict) -> tuple[bool, str | None]:
    hostname = (urlsplit(str(candidate.url)).hostname or "").lower()
    allowed_domains = search_config.get("allowed_domains") or []
    blocked_domains = search_config.get("blocked_domains") or []
    if allowed_domains and not any(_domain_matches(hostname, rule) for rule in allowed_domains):
        return False, "domain is outside the configured allow-list"
    if any(_domain_matches(hostname, rule) for rule in blocked_domains):
        return False, "domain is on the configured block-list"
    categories = search_config.get("source_categories") or []
    if categories and candidate.source_category not in categories:
        return False, "source category is outside the configured categories"
    haystack = f"{candidate.name} {candidate.url} {candidate.reasoning}".casefold()
    if any(term.casefold() in haystack for term in search_config.get("excluded_terms") or []):
        return False, "candidate matched an excluded term"
    minimum = float(search_config.get("min_confidence", 0.0))
    if candidate.confidence < minimum:
        return False, "candidate confidence is below the configured minimum"
    return True, None


def activate_proposal(
    session: Session,
    settings: Settings,
    proposal: SourceProposal,
    *,
    actor: str,
) -> bool:
    source = session.get(SourceDefinition, proposal.source_definition_id)
    if source is None:
        proposal.status = ProposalStatus.INVALID
        proposal.review_note = "source record is missing"
        proposal.reviewed_at = utc_now()
        return False
    if source.access_method != "manual":
        check_robots(settings, source)
    blockers = activation_blockers(source, enforce_robots=robots_enforced(source, settings))
    if blockers:
        proposal.review_note = "; ".join(blockers)
        return False
    source.lifecycle_status = SourceLifecycle.ACTIVE
    source.updated_at = utc_now()
    proposal.status = (
        ProposalStatus.AUTO_ACTIVATED if actor == "source-scout" else ProposalStatus.ACCEPTED
    )
    proposal.reviewed_at = utc_now()
    proposal.review_note = "policy-cleared and activated"
    audit.record(
        session,
        actor,
        "source_scout.proposal_activated",
        "source_proposal",
        proposal.id,
        {"source_id": source.id},
    )
    if source.access_method in {"html", "sitemap"}:
        jobs.enqueue(
            session,
            "crawler.crawl_source",
            payload={"source_id": source.id},
            priority=PriorityClass.LOW_PRIORITY_DISCOVERY,
            actor=actor,
        )
    elif source.access_method != "manual":
        jobs.enqueue(
            session,
            "sources.collect",
            payload={"source_id": source.id},
            priority=PriorityClass.LOW_PRIORITY_DISCOVERY,
            actor=actor,
        )
    return True


def _ingest_result(
    session: Session,
    settings: Settings,
    run: ResearchRun,
    result: SourceExpansionResult,
) -> dict:
    limit = max(1, min(int(run.plan_snapshot["budgets"].get("max_candidates", 25)), 200))
    counts = {"proposed": 0, "duplicate": 0, "invalid": 0, "auto_activated": 0}
    seen: set[str] = set()
    search_config = run.plan_snapshot.get("search_config") or {}
    for candidate in result.candidates[:limit]:
        try:
            allowed, rejection = _candidate_allowed(candidate, search_config)
            if not allowed:
                counts["invalid"] += 1
                audit.record(
                    session,
                    "source-scout",
                    "source_scout.candidate_filtered",
                    detail={
                        "run_id": run.id,
                        "url": str(candidate.url),
                        "reason": rejection,
                    },
                )
                continue
            normalised = url_identity(str(candidate.url)).normalised_value
            if normalised in seen:
                counts["duplicate"] += 1
                continue
            seen.add(normalised)
            proposal = _create_proposal(session, run, candidate)
            if proposal.status == ProposalStatus.DUPLICATE:
                counts["duplicate"] += 1
                continue
            counts["proposed"] += 1
            if run.plan_snapshot[
                "activation_mode"
            ] == ActivationMode.AUTO_ACTIVATE and activate_proposal(
                session, settings, proposal, actor="source-scout"
            ):
                counts["auto_activated"] += 1
        except (ValueError, TypeError) as exc:
            counts["invalid"] += 1
            audit.record(
                session,
                "source-scout",
                "source_scout.candidate_invalid",
                detail={"run_id": run.id, "error": str(exc)[:500]},
            )
    return counts


def execute_run(
    engine: Engine,
    settings: Settings,
    run_id: str,
    *,
    provider: SourceScoutProvider | None = None,
) -> dict:
    with session_scope(engine) as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise ValueError(f"research run not found: {run_id}")
        run.status = ScoutRunStatus.RUNNING
        run.started_at = utc_now()
        run.error = None
        request = _request_for_run(session, run)
        prompt = build_source_expansion_prompt(request)
        invocation = AIInvocation(
            run_id=run.id,
            prompt_version=PROMPT_VERSION,
            provider=run.provider,
            model=run.model,
            input_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            input_payload=request.model_dump(mode="json"),
        )
        session.add(invocation)
        session.flush()
        invocation_id = invocation.id
        provider_name = run.provider
        model = run.model
        budgets = dict(run.plan_snapshot.get("budgets") or {})
    try:
        selected = provider or get_provider(settings, provider_name)
        response = selected.complete(
            prompt,
            model=model,
            budgets=budgets,
            cancelled=lambda: _cancel_requested(engine, run_id),
        )
        with session_scope(engine) as session:
            run = session.get(ResearchRun, run_id)
            invocation = session.get(AIInvocation, invocation_id)
            invocation.raw_output = response.raw_output
            invocation.validated_output = response.output.model_dump(mode="json")
            invocation.validation_status = "valid"
            invocation.input_tokens = response.input_tokens
            invocation.output_tokens = response.output_tokens
            invocation.cost_usd = response.cost_usd
            invocation.finished_at = utc_now()
            counts = _ingest_result(session, settings, run, response.output)
            run.status = ScoutRunStatus.SUCCEEDED
            run.counters = counts
            run.summary = {
                "summary": response.output.summary,
                "queries_used": response.output.queries_used,
                "coverage_gaps": response.output.coverage_gaps,
                "explicit_unknowns": response.output.explicit_unknowns,
            }
            run.finished_at = utc_now()
        return {"run_id": run_id, **counts}
    except ScoutCancelled as exc:
        with session_scope(engine) as session:
            run = session.get(ResearchRun, run_id)
            invocation = session.get(AIInvocation, invocation_id)
            run.status = ScoutRunStatus.CANCELLED
            run.error = str(exc)
            run.finished_at = utc_now()
            invocation.validation_status = "failed"
            invocation.error = str(exc)
            invocation.finished_at = utc_now()
        raise
    except Exception as exc:
        with session_scope(engine) as session:
            run = session.get(ResearchRun, run_id)
            invocation = session.get(AIInvocation, invocation_id)
            job = session.get(Job, run.job_id) if run.job_id else None
            retrying = bool(job and job.attempts < job.max_attempts and not job.cancel_requested)
            run.status = ScoutRunStatus.QUEUED if retrying else ScoutRunStatus.FAILED
            run.error = str(exc)[:20_000]
            run.finished_at = None if retrying else utc_now()
            invocation.validation_status = "failed"
            invocation.error = str(exc)[:20_000]
            invocation.finished_at = utc_now()
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError(str(exc)) from exc
