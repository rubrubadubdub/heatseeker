"""Source registry jobs: seed sync, policy checks, collection (M2)."""

from heatseeker_common.db import session_scope
from heatseeker_common.job_registry import JobContext, PermanentJobError, job_handler
from heatseeker_industry_packs.loader import default_packs_root, load_pack
from heatseeker_source_registry.collect import collect_source
from heatseeker_source_registry.models import SourceCoverage, SourceDefinition
from heatseeker_source_registry.policy import (
    check_coverage_robots,
    check_robots,
    coverage_has_distinct_endpoint,
)
from heatseeker_source_registry.sync import sync_pack_seeds


@job_handler("sources.sync_pack_seeds")
def sync_seeds(ctx: JobContext) -> dict:
    pack_id = ctx.payload["pack_id"]
    pack = load_pack(default_packs_root() / pack_id)
    expected_version = ctx.payload.get("pack_version")
    expected_hash = ctx.payload.get("pack_hash")
    if expected_version and expected_version != pack.version:
        raise PermanentJobError(
            f"pack version changed: expected {expected_version}, found {pack.version}"
        )
    if expected_hash and expected_hash != pack.content_hash:
        raise PermanentJobError("pack content changed after the sync job was enqueued")
    with session_scope(ctx.engine) as session:
        return sync_pack_seeds(session, pack, actor="worker")


@job_handler("sources.check_policy")
def check_policy(ctx: JobContext) -> dict:
    settings = ctx.settings
    source_id = ctx.payload["source_id"]
    coverage_id = ctx.payload.get("coverage_id")
    with session_scope(ctx.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            raise PermanentJobError(f"source not found: {source_id}")
        if coverage_id:
            coverage = session.get(SourceCoverage, coverage_id)
            if coverage is None or coverage.source_definition_id != source_id:
                raise PermanentJobError(f"source coverage not found: {coverage_id}")
            status = check_coverage_robots(settings, source, coverage)
        else:
            status = check_robots(settings, source)
        return {
            "source": source.name,
            "source_id": source.id,
            "coverage_id": coverage_id,
            "robots_status": str(status),
        }


@job_handler("sources.check_policy_all")
def check_policy_all(ctx: JobContext) -> dict:
    """Policy-check every source that hasn't been checked yet (candidates first)."""
    settings = ctx.settings
    results: dict[str, str] = {}
    results_by_id: dict[str, dict[str, str]] = {}
    with session_scope(ctx.engine) as session:
        sources = list(
            session.query(SourceDefinition)
            .filter(SourceDefinition.robots_status.in_(["unknown", "unreachable"]))
            .all()
        )
        for source in sources:
            status = str(check_robots(settings, source))
            results[source.name] = status
            results_by_id[source.id] = {"name": source.name, "robots_status": status}
        coverages = list(
            session.query(SourceCoverage)
            .filter(SourceCoverage.robots_status.in_(["unknown", "unreachable"]))
            .all()
        )
        for coverage in coverages:
            source = session.get(SourceDefinition, coverage.source_definition_id)
            if not coverage_has_distinct_endpoint(source, coverage):
                continue
            status = str(check_coverage_robots(settings, source, coverage))
            results_by_id[coverage.id] = {
                "name": f"{source.name} / {coverage.name}",
                "robots_status": status,
            }
    return {"checked": len(results_by_id), "results": results, "results_by_id": results_by_id}


@job_handler("crawler.crawl_source")
def crawl(ctx: JobContext) -> dict:
    """Crawl one source's site within budgets (M3)."""
    from heatseeker_source_registry.crawler import CrawlBudget, crawl_source

    settings = ctx.settings
    budget = CrawlBudget.from_settings(
        settings,
        max_pages=ctx.payload.get("max_pages"),
        max_depth=ctx.payload.get("max_depth"),
    )
    with session_scope(ctx.engine) as session:
        result = crawl_source(
            session,
            settings,
            ctx.payload["source_id"],
            budget=budget,
            release_between_fetches=True,
        )
    if result["outcome"] == "error":
        raise PermanentJobError(result.get("error", "invalid crawl request"))
    return result


@job_handler("sources.autopilot")
def autopilot(ctx: JobContext) -> dict:
    """One self-driving tick: seed, policy-check, activate, collect, maintain."""
    from heatseeker_source_registry.autopilot import autopilot_tick

    settings = ctx.settings
    with session_scope(ctx.engine) as session:
        return autopilot_tick(session, settings)


@job_handler("sources.evaluate_all")
def evaluate_all_sources(ctx: JobContext) -> dict:
    """Grade every source and apply auto-deprecation rules (grading.py)."""
    from heatseeker_source_registry.grading import evaluate_all

    with session_scope(ctx.engine) as session:
        return evaluate_all(session)


@job_handler("sources.recheck_policies")
def recheck_policies(ctx: JobContext) -> dict:
    """Re-evaluate robots for sources whose decision is stale (spec §11.3: periodic)."""
    from datetime import timedelta

    from heatseeker_common.timeutil import utc_now

    settings = ctx.settings
    cutoff = utc_now() - timedelta(days=settings.robots_recheck_days)
    rechecked: dict[str, str] = {}
    with session_scope(ctx.engine) as session:
        stale = list(
            session.query(SourceDefinition).filter(
                SourceDefinition.access_method != "manual",
                SourceDefinition.robots_checked_at.isnot(None),
                SourceDefinition.robots_checked_at < cutoff,
            )
        )
        for source in stale:
            rechecked[source.name] = str(check_robots(settings, source))
    return {"rechecked": len(rechecked), "results": rechecked}


@job_handler("sources.collect_due")
def collect_due_sources(ctx: JobContext) -> dict:
    """Collect every source whose adaptive schedule says it is due, politely."""
    from heatseeker_source_registry.schedule import collect_due

    settings = ctx.settings
    limit = ctx.payload.get("limit")
    with session_scope(ctx.engine) as session:
        return collect_due(
            session,
            settings,
            limit=limit,
            release_between_fetches=True,
        )


@job_handler("sources.collect")
def collect(ctx: JobContext) -> dict:
    settings = ctx.settings
    source_id = ctx.payload["source_id"]
    coverage_id = ctx.payload.get("coverage_id")
    pairing_ids = ctx.payload.get("pairing_ids") or []
    if coverage_id and pairing_ids and coverage_id not in pairing_ids:
        raise PermanentJobError("coverage_id and pairing_ids disagree")
    if not coverage_id and len(pairing_ids) == 1:
        coverage_id = pairing_ids[0]
    if len(pairing_ids) > 1:
        raise PermanentJobError("one collection job must resolve to one coherent coverage/request")
    scope_snapshot = ctx.payload.get("scope_snapshot")
    if scope_snapshot is not None and not isinstance(scope_snapshot, dict):
        raise PermanentJobError("scope_snapshot must be an object")
    with session_scope(ctx.engine) as session:
        result = collect_source(
            session,
            settings,
            source_id,
            coverage_id=coverage_id,
            scope_snapshot=scope_snapshot,
            release_before_fetch=True,
        )
    if result["outcome"] == "error":
        raise PermanentJobError(result.get("error", "invalid collection request"))
    if result["outcome"] == "failure":
        # Raise so the job retries with backoff; source health was already recorded.
        raise RuntimeError(result.get("error", "collection failed"))
    return result
