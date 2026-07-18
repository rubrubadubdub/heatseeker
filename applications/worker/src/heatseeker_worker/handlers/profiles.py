"""Autonomous company research: public-web lookup, verification, and deep fetch."""

import hashlib

from heatseeker_ai.prompts import (
    ENTITY_RESEARCH_PROMPT_VERSION,
    build_entity_research_prompt,
)
from heatseeker_ai.providers import ProviderError, ScoutCancelled, get_provider, provider_health
from heatseeker_common import audit, jobs
from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import (
    JobCancelled,
    JobContext,
    PermanentJobError,
    job_handler,
)
from heatseeker_common.models import Job, PriorityClass
from heatseeker_entity_resolution.models import Organisation
from heatseeker_intelligence.company_profiles import (
    entity_research_queries,
    entity_research_snapshot,
    fetch_and_extract,
    verify_and_attach_domain,
)
from heatseeker_intelligence.domain_discovery import discover_domain
from heatseeker_intelligence.research_requirements import assess
from sqlalchemy import select


def _queue_rescores(session, actor: str) -> int:
    from heatseeker_lead_intelligence.models import Offering, OfferingStatus

    queued = 0
    for offering_id in session.scalars(
        select(Offering.id).where(Offering.status == OfferingStatus.ACTIVE)
    ):
        pending = session.scalars(
            select(Job.id).where(
                Job.job_type == "leads.rescore",
                Job.status.in_(["queued", "running"]),
                Job.payload["offering_id"].as_string() == offering_id,
            )
        ).first()
        if pending is None:
            jobs.enqueue(
                session,
                "leads.rescore",
                payload={"offering_id": offering_id},
                priority=PriorityClass.BACKGROUND_ENRICHMENT,
                actor=actor,
            )
            queued += 1
    return queued


@job_handler("profiles.fetch")
def profile_fetch(ctx: JobContext) -> dict:
    organisation_id = ctx.payload.get("organisation_id")
    if not isinstance(organisation_id, str) or not organisation_id:
        raise PermanentJobError("profiles.fetch requires organisation_id")
    with session_scope(ctx.engine) as session:
        try:
            summary = fetch_and_extract(session, ctx.settings, organisation_id)
            summary["lead_rescores_queued"] = _queue_rescores(session, "profile-fetch")
            return summary
        except LookupError as exc:
            raise PermanentJobError(str(exc)) from exc


@job_handler("profiles.research")
def profile_research(ctx: JobContext) -> dict:
    """Find a missing official site, verify it, then run the deterministic deep fetch."""
    organisation_id = ctx.payload.get("organisation_id")
    if not isinstance(organisation_id, str) or not organisation_id:
        raise PermanentJobError("profiles.research requires organisation_id")
    with session_scope(ctx.engine) as session:
        organisation = session.get(Organisation, organisation_id)
        if organisation is None:
            raise PermanentJobError(f"organisation not found: {organisation_id}")
        report = assess(session, organisation)
        snapshot = entity_research_snapshot(organisation, report.missing)
        queries = entity_research_queries(organisation, report.missing)
        deterministic_lookup = discover_domain(
            session, ctx.settings, organisation_id, max_candidates=12
        )
        if deterministic_lookup.get("status") == "discovered":
            deep_fetch = fetch_and_extract(session, ctx.settings, organisation_id)
            rescores = _queue_rescores(session, "deterministic-domain-discovery")
            final_report = assess(session, organisation)
            return {
                "organisation_id": organisation_id,
                "provider": "deterministic",
                "queries": [],
                "candidates_checked": deterministic_lookup["candidates_tried"],
                "accepted_domain": deterministic_lookup["domain"],
                "verification_attempts": [],
                "deep_fetch": deep_fetch,
                "completion": {
                    "complete": final_report.complete,
                    "score": final_report.score,
                    "missing": list(final_report.missing),
                },
                "lead_rescores_queued": rescores,
            }
        if not ctx.settings.ai_enabled:
            return {
                "organisation_id": organisation_id,
                "provider": "disabled",
                "queries": [],
                "candidates_checked": deterministic_lookup["candidates_tried"],
                "accepted_domain": None,
                "verification_attempts": [],
                "deep_fetch": None,
                "completion": {
                    "complete": report.complete,
                    "score": report.score,
                    "missing": list(report.missing),
                },
                "lead_rescores_queued": 0,
            }
    prompt = build_entity_research_prompt(snapshot, queries)

    provider_name = next(
        (
            name
            for name in ("codex", "claude")
            if (health := provider_health(ctx.settings, name)).installed
            and health.authenticated
        ),
        None,
    )
    if provider_name is None:
        raise ProviderError("no installed and authenticated web-research provider")
    provider = get_provider(ctx.settings, provider_name)

    def cancelled() -> bool:
        with session_scope(ctx.engine) as session:
            job = session.get(Job, ctx.job_id)
            return bool(job and job.cancel_requested)

    try:
        result = provider.research_entity(
            prompt,
            model=None,
            budgets={"max_turns": 12, "timeout_seconds": 900, "max_budget_usd": 3.0},
            cancelled=cancelled,
        )
    except ScoutCancelled as exc:
        raise JobCancelled(str(exc)) from exc

    accepted = None
    attempts = []
    official_types = {
        "official_website",
        "official_contact",
        "official_services",
        "official_location",
    }
    candidates = sorted(
        (
            candidate
            for candidate in result.output.candidates
            if candidate.page_type in official_types
        ),
        key=lambda candidate: candidate.confidence,
        reverse=True,
    )
    with session_scope(ctx.engine) as session:
        for candidate in candidates[:12]:
            verification = verify_and_attach_domain(
                session, ctx.settings, organisation_id, str(candidate.url)
            )
            attempts.append(verification)
            if verification.get("accepted"):
                accepted = verification
                break
        audit.record(
            session,
            "entity-research",
            "profile.web_research_completed",
            "organisation",
            organisation_id,
            {
                "prompt_version": ENTITY_RESEARCH_PROMPT_VERSION,
                "input_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                "provider": provider_name,
                "queries": list(result.output.queries_used),
                "candidate_urls": [str(candidate.url) for candidate in candidates],
                "verification_attempts": attempts,
                "unknowns": list(result.output.explicit_unknowns),
            },
        )
        deep_fetch = None
        if accepted:
            deep_fetch = fetch_and_extract(session, ctx.settings, organisation_id)
        rescores = _queue_rescores(session, "entity-research")
        final_report = assess(session, session.get(Organisation, organisation_id))
    return {
        "organisation_id": organisation_id,
        "provider": provider_name,
        "queries": list(result.output.queries_used),
        "candidates_checked": len(attempts),
        "accepted_domain": accepted.get("domain") if accepted else None,
        "verification_attempts": attempts,
        "deep_fetch": deep_fetch,
        "completion": {
            "complete": final_report.complete,
            "score": final_report.score,
            "missing": list(final_report.missing),
        },
        "lead_rescores_queued": rescores,
    }
