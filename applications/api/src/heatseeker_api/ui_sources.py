"""GUI routes for sources, evidence, and research scopes (M2)."""

import html
import json
import re
import uuid
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from heatseeker_common import audit, jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import PriorityClass
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import KNOWN_CODES, describe, normalise_codes
from heatseeker_industry_packs.loader import (
    PackValidationError,
    discover_packs,
    load_pack,
)
from heatseeker_source_registry.identity import (
    attach_identity,
    resolve_identities,
    url_identity,
)
from heatseeker_source_registry.models import (
    GeoRegion,
    ResearchScope,
    SourceCoverage,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
    SourceRelationship,
    TermsStatus,
)
from heatseeker_source_registry.policy import (
    activation_blockers,
    check_coverage_robots,
    check_robots,
    coverage_has_distinct_endpoint,
    robots_enforced,
)
from heatseeker_source_registry.regions import delete_region, load_regions, upsert_region
from heatseeker_source_registry.scopes import (
    active_scope,
    create_scope,
    ensure_default_scopes,
    set_active,
    source_in_scope,
)
from heatseeker_source_registry.sync import sync_pack_seeds
from heatseeker_source_registry.targeting import (
    CoverageSpec,
    CoverageValidationError,
    TargetSpec,
    disable_coverage,
    match_coverages,
    upsert_coverage,
)
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from heatseeker_api.ui_routes import _redirect, _render

router = APIRouter(include_in_schema=False)

LIFECYCLE_BADGES = {
    "proposed": "info",
    "candidate": "secondary",
    "active": "success",
    "degraded": "warning",
    "disabled": "dark",
    "deprecated": "danger",
    "rejected": "danger",
}
GRADE_BADGES = {
    "A": "success",
    "B": "success",
    "C": "warning",
    "D": "warning",
    "E": "danger",
    "U": "secondary",
}
POLICY_BADGES = {
    "allowed": "success",
    "not_applicable": "secondary",
    "disallowed": "danger",
    "unknown": "secondary",
    "unreachable": "warning",
    "unreviewed": "secondary",
    "approved": "success",
    "unclear": "warning",
    "prohibited": "danger",
}


# --- Sources -------------------------------------------------------------------


@router.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    scope_only: bool = Query(default=False),
    industry_id: str | None = Query(default=None),
    region_code: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    dimension: str | None = Query(default=None),
    target: str | None = Query(default=None),
    include_unknown: bool = Query(default=False),
):
    engine = request.app.state.engine
    settings = request.app.state.settings
    with session_scope(engine) as session:
        ensure_default_scopes(session)
        scope = active_scope(session)
        stmt = (
            select(SourceDefinition)
            .options(selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets))
            .order_by(SourceDefinition.authority_tier, SourceDefinition.name)
        )
        if category:
            stmt = stmt.where(SourceDefinition.source_category == category)
        if status:
            stmt = stmt.where(SourceDefinition.lifecycle_status == status)
        rows = list(session.scalars(stmt))
        target_filters = {dimension: [target]} if dimension and target else {}
        items = []
        for source in rows:
            in_scope_flag = source_in_scope(source, scope)
            if scope_only and not in_scope_flag:
                continue
            match = match_coverages(
                source.coverages,
                industry_ids=[industry_id] if industry_id else (),
                region_codes=[region_code] if region_code else (),
                target_filters=target_filters,
                include_unknown=include_unknown,
            )
            has_filters = bool(industry_id or region_code or target_filters)
            if has_filters and not match:
                continue
            items.append(
                {
                    "source": source,
                    "in_scope": in_scope_flag,
                    "blockers": activation_blockers(
                        source,
                        enforce_robots=robots_enforced(source, settings),
                    ),
                    "matching_count": len(match.matched_coverage_keys),
                    "coverage_count": sum(
                        bool(match_coverages([coverage])) for coverage in source.coverages
                    ),
                }
            )
        doc_counts = dict(
            session.execute(
                select(SourceDocument.source_definition_id, func.count(SourceDocument.id)).group_by(
                    SourceDocument.source_definition_id
                )
            ).all()
        )
        scope_name = scope.name if scope else None
        scope_codes = list(scope.geo_codes) if scope else []
        scope_industries = list(scope.industry_ids) if scope else []
        industry_choices = sorted(
            {
                target.target_key
                for source in rows
                for coverage in source.coverages
                for target in coverage.targets
                if target.dimension == "industry" and target.polarity == "include"
            }
        )
        region_choices = sorted(
            {
                target.target_key
                for source in rows
                for coverage in source.coverages
                for target in coverage.targets
                if target.dimension == "region" and target.polarity == "include"
            }
        )
        category_choices = sorted({source.source_category for source in rows})
        session.expunge_all()
    return _render(
        request,
        "sources.html",
        active="sources",
        items=items,
        doc_counts=doc_counts,
        scope_name=scope_name,
        scope_codes=scope_codes,
        scope_industries=scope_industries,
        scope_only=scope_only,
        industry_id=industry_id or "",
        region_code=region_code or "",
        category_filter=category or "",
        status_filter=status or "",
        dimension_filter=dimension or "",
        target_filter=target or "",
        include_unknown=include_unknown,
        industry_choices=industry_choices,
        region_choices=region_choices,
        category_choices=category_choices,
        lifecycle_choices=[lifecycle.value for lifecycle in SourceLifecycle],
        lifecycle_badges=LIFECYCLE_BADGES,
        grade_badges=GRADE_BADGES,
        policy_badges=POLICY_BADGES,
        terms_choices=[t.value for t in TermsStatus],
        robots_policy=settings.robots_policy,
    )


@router.post("/sources/sync-seeds")
def sources_sync_seeds(request: Request):
    engine = request.app.state.engine
    totals = {
        "sources_created": 0,
        "sources_updated": 0,
        "coverages_created": 0,
        "coverages_updated": 0,
        "coverages_disabled": 0,
        "conflicts": 0,
    }
    with session_scope(engine) as session:
        for pack_path in discover_packs():
            try:
                pack = load_pack(pack_path)
            except PackValidationError:
                continue
            result = sync_pack_seeds(session, pack, actor="ui")
            for key in totals:
                if key == "conflicts":
                    totals[key] += len(result.get(key, []))
                else:
                    totals[key] += result.get(key, 0)
    level = "warning" if totals["conflicts"] else "success"
    return _redirect(
        "/sources",
        "Seed sync: "
        f"{totals['sources_created']} sources created, "
        f"{totals['sources_updated']} changed; "
        f"{totals['coverages_created']} coverage profiles created, "
        f"{totals['coverages_updated']} changed, "
        f"{totals['coverages_disabled']} disabled; "
        f"{totals['conflicts']} conflicts",
        level,
    )


@router.post("/sources/create")
def sources_create(
    request: Request,
    name: str = Form(...),
    source_category: str = Form(...),
    base_url: str = Form(default=""),
    access_method: str = Form(default="html"),
    authority_tier: int = Form(default=5),
    geo_codes: str = Form(default=""),
    language: str = Form(default=""),
    expected_update_frequency: str = Form(default=""),
    authentication_type: str = Form(default=""),
    parser_profile: str = Form(default=""),
    notes: str = Form(default=""),
):
    name = name.strip()
    category = source_category.strip().lower().replace(" ", "_")
    url = base_url.strip() or None
    if (
        not name
        or len(name) > 300
        or len(category) > 50
        or not re.fullmatch(r"[a-z][a-z0-9_]*", category)
    ):
        return _redirect("/sources", "Name and a valid category are required", "danger")
    if access_method not in {"api", "bulk", "rss", "sitemap", "html", "rendered", "manual"}:
        return _redirect("/sources", "Invalid access method", "danger")
    if not 1 <= authority_tier <= 7:
        return _redirect("/sources", "Authority tier must be between 1 and 7", "danger")
    if access_method != "manual" and not url:
        return _redirect("/sources", "Automated sources require a URL", "danger")
    if url:
        if len(url) > 1000:
            return _redirect("/sources", "URL is too long", "danger")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return _redirect("/sources", "URL must be absolute HTTP(S)", "danger")
        if parts.username or parts.password:
            return _redirect("/sources", "URL must not contain credentials", "danger")
    try:
        codes = normalise_codes(
            [value for value in geo_codes.replace(";", ",").split(",") if value.strip()],
            validate=True,
        )
    except ValueError as exc:
        return _redirect("/sources", str(exc), "danger")
    with session_scope(request.app.state.engine) as session:
        identity = url_identity(url) if url else None
        if identity and resolve_identities(session, [identity]) is not None:
            return _redirect("/sources", "A source with that URL already exists", "warning")
        source = SourceDefinition(
            name=name,
            source_category=category,
            base_url=url,
            jurisdiction=", ".join(codes) or None,
            geo_codes=codes,
            language=language.strip() or None,
            access_method=access_method,
            authority_tier=authority_tier,
            expected_update_frequency=expected_update_frequency.strip() or None,
            authentication_type=authentication_type.strip() or None,
            parser_profile=parser_profile.strip() or None,
            origin="user",
            notes=notes.strip() or None,
        )
        session.add(source)
        session.flush()
        if identity:
            attach_identity(session, source, identity, origin="user", is_primary=True)
        audit.record(
            session,
            "ui",
            "source.created",
            "source",
            source.id,
            {"name": name, "base_url": url},
        )
        source_id = source.id
    return _redirect(f"/sources/{source_id}", f"Created source {name}")


@router.post("/sources/{source_id}/edit")
def sources_edit(
    request: Request,
    source_id: str,
    name: str = Form(...),
    source_category: str = Form(...),
    base_url: str = Form(default=""),
    access_method: str = Form(...),
    authority_tier: int = Form(...),
    geo_codes: str = Form(default=""),
    language: str = Form(default=""),
    expected_update_frequency: str = Form(default=""),
    authentication_type: str = Form(default=""),
    parser_profile: str = Form(default=""),
    notes: str = Form(default=""),
):
    name = name.strip()
    category = source_category.strip().lower().replace(" ", "_")
    url = base_url.strip() or None
    if (
        not name
        or len(name) > 300
        or len(category) > 50
        or not re.fullmatch(r"[a-z][a-z0-9_]*", category)
    ):
        return _redirect(f"/sources/{source_id}", "Invalid name/category", "danger")
    if access_method not in {
        "api",
        "bulk",
        "rss",
        "sitemap",
        "html",
        "rendered",
        "manual",
    }:
        return _redirect(f"/sources/{source_id}", "Invalid access method", "danger")
    if access_method != "manual" and not url:
        return _redirect(f"/sources/{source_id}", "URL is required", "danger")
    if url and len(url) > 1000:
        return _redirect(f"/sources/{source_id}", "URL is too long", "danger")
    if not 1 <= authority_tier <= 7:
        return _redirect(f"/sources/{source_id}", "Invalid authority tier", "danger")
    try:
        codes = normalise_codes(
            [value for value in geo_codes.replace(";", ",").split(",") if value.strip()],
            validate=True,
        )
        identity = url_identity(url) if url else None
    except ValueError as exc:
        return _redirect(f"/sources/{source_id}", str(exc), "danger")
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        if identity:
            resolved = resolve_identities(session, [identity])
            if resolved is not None and resolved.id != source_id:
                return _redirect(f"/sources/{source_id}", "URL belongs to another source", "danger")
        old_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
        old_access_method = source.access_method
        changes = {
            "name": name,
            "source_category": category,
            "base_url": url,
            "jurisdiction": ", ".join(codes) or None,
            "geo_codes": codes,
            "language": language.strip() or None,
            "access_method": access_method,
            "authority_tier": authority_tier,
            "expected_update_frequency": expected_update_frequency.strip() or None,
            "authentication_type": authentication_type.strip() or None,
            "parser_profile": parser_profile.strip() or None,
            "notes": notes.strip() or None,
        }
        changed_fields = []
        for field_name, value in changes.items():
            if getattr(source, field_name) != value:
                setattr(source, field_name, value)
                changed_fields.append(field_name)
        new_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
        if old_policy_url != new_policy_url:
            source.robots_status = "unknown"
            source.robots_checked_at = None
        if old_access_method != source.access_method:
            source.robots_status = "unknown"
            source.robots_checked_at = None
            for coverage in source.coverages:
                coverage.robots_status = "unknown"
                coverage.robots_checked_at = None
                coverage.updated_at = utc_now()
        if identity:
            attach_identity(
                session,
                source,
                identity,
                origin="user",
                is_primary=not source.identities,
            )
        if changed_fields:
            source.updated_at = utc_now()
            audit.record(
                session,
                "ui",
                "source.updated",
                "source",
                source.id,
                {"changed_fields": changed_fields},
            )
    return _redirect(
        f"/sources/{source_id}",
        "Source updated" if changed_fields else "No source changes",
    )


@router.post("/sources/run-maintenance")
def sources_run_maintenance(request: Request):
    """Grade + auto-deprecate all sources and re-check stale robots decisions."""
    with session_scope(request.app.state.engine) as session:
        evaluate = jobs.enqueue(
            session, "sources.evaluate_all", priority=PriorityClass.INTERACTIVE, actor="ui"
        )
        jobs.enqueue(
            session, "sources.recheck_policies", priority=PriorityClass.INTERACTIVE, actor="ui"
        )
        job_id = evaluate.id
    return _redirect(
        "/sources",
        f"Maintenance running: grading, auto-deprecation, robots re-check (job {job_id[:8]})",
    )


@router.post("/sources/collect-due")
def sources_collect_due(request: Request):
    with session_scope(request.app.state.engine) as session:
        job = jobs.enqueue(
            session, "sources.collect_due", priority=PriorityClass.INTERACTIVE, actor="ui"
        )
        job_id = job.id
    return _redirect(
        "/sources", f"Collecting all due sources with politeness delays (job {job_id[:8]})"
    )


@router.post("/sources/{source_id}/crawl")
def sources_crawl(request: Request, source_id: str):
    with session_scope(request.app.state.engine) as session:
        job = jobs.enqueue(
            session,
            "crawler.crawl_source",
            payload={"source_id": source_id},
            priority=PriorityClass.INTERACTIVE,
            actor="ui",
        )
        job_id = job.id
    return _redirect(
        "/sources",
        f"Crawl queued (job {job_id[:8]}) - budgeted site walk; results under Evidence",
    )


@router.post("/sources/{source_id}/reinstate")
def sources_reinstate(request: Request, source_id: str):
    from heatseeker_source_registry.grading import reinstate

    with session_scope(request.app.state.engine) as session:
        source = reinstate(session, source_id, actor="ui")
        if source is None:
            return _redirect("/sources", "Source is not deprecated or not found", "warning")
        name = source.name
    return _redirect("/sources", f"Reinstated {name} as candidate — re-check policy before use")


@router.post("/sources/{source_id}/check-policy")
def sources_check_policy(request: Request, source_id: str):
    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        status = check_robots(settings, source)
        name = source.name
    return _redirect("/sources", f"Robots for {name}: {status}")


@router.post("/sources/check-all-policies")
def sources_check_all(request: Request):
    with session_scope(request.app.state.engine) as session:
        job = jobs.enqueue(
            session, "sources.check_policy_all", priority=PriorityClass.INTERACTIVE, actor="ui"
        )
        job_id = job.id
    return _redirect(
        "/sources", f"Policy check running for all unchecked sources (job {job_id[:8]})"
    )


@router.post("/sources/{source_id}/activate")
def sources_activate(request: Request, source_id: str):
    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        blockers = activation_blockers(
            source,
            enforce_robots=robots_enforced(source, settings),
        )
        if blockers:
            return _redirect("/sources", f"Cannot activate: {'; '.join(blockers)}", "warning")
        source.lifecycle_status = SourceLifecycle.ACTIVE
        source.updated_at = utc_now()
        audit.record(session, "ui", "source.activated", "source", source.id, {"name": source.name})
        name = source.name
    return _redirect("/sources", f"Activated {name}")


@router.post("/sources/{source_id}/robots-policy")
def sources_set_robots_policy(
    request: Request,
    source_id: str,
    robots_policy: str = Form(...),
):
    choices = {"global": None, "enforce": True, "ignore": False}
    if robots_policy not in choices:
        return _redirect(f"/sources/{source_id}", "Invalid robots policy", "danger")
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        source.respect_robots_override = choices[robots_policy]
        source.updated_at = utc_now()
        audit.record(
            session,
            "ui",
            "source.robots_policy_changed",
            "source",
            source.id,
            {"override": robots_policy},
        )
        name = source.name
    return _redirect(f"/sources/{source_id}", f"Robots policy for {name}: {robots_policy}")


@router.post("/sources/{source_id}/deactivate")
def sources_deactivate(request: Request, source_id: str):
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        source.lifecycle_status = SourceLifecycle.DISABLED
        source.updated_at = utc_now()
        audit.record(session, "ui", "source.disabled", "source", source.id)
        name = source.name
    return _redirect("/sources", f"Disabled {name}", "warning")


@router.post("/sources/{source_id}/terms")
def sources_set_terms(request: Request, source_id: str, terms_status: str = Form(...)):
    if terms_status not in [t.value for t in TermsStatus]:
        return _redirect("/sources", "Invalid terms status", "danger")
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        source.terms_status = terms_status
        source.updated_at = utc_now()
        audit.record(
            session,
            "ui",
            "source.terms_reviewed",
            "source",
            source.id,
            {"terms_status": terms_status},
        )
        name = source.name
    return _redirect("/sources", f"Terms for {name}: {terms_status}")


@router.post("/sources/{source_id}/collect")
def sources_collect(
    request: Request,
    source_id: str,
    coverage_id: str = Form(default=""),
):
    settings = request.app.state.settings
    # Keep the validation/snapshot read separate from the queue write.  In SQLite
    # WAL mode, upgrading a read transaction after another process has committed
    # can fail with SQLITE_BUSY_SNAPSHOT; busy_timeout cannot make that stale
    # snapshot writable.  A fresh transaction can wait normally for the writer.
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        if source.lifecycle_status not in (
            SourceLifecycle.ACTIVE,
            SourceLifecycle.DEGRADED,
        ):
            return _redirect(
                f"/sources/{source_id}",
                f"Source is {source.lifecycle_status}, not collectable",
                "warning",
            )
        coverage = session.get(SourceCoverage, coverage_id) if coverage_id else None
        if coverage_id and (coverage is None or coverage.source_definition_id != source_id):
            return _redirect(f"/sources/{source_id}", "Coverage profile not found", "danger")
        blockers = activation_blockers(
            source,
            coverage,
            enforce_robots=robots_enforced(source, settings),
        )
        if blockers:
            return _redirect(
                f"/sources/{source_id}",
                f"Collection blocked: {'; '.join(blockers)}",
                "warning",
            )
        scope = active_scope(session)
        scope_snapshot = (
            {
                "id": scope.id,
                "name": scope.name,
                "geo_codes": list(scope.geo_codes),
                "industry_ids": list(scope.industry_ids),
                "target_filters": dict(scope.target_filters),
                "include_unknown": scope.include_unknown,
            }
            if scope
            else None
        )
        payload = {
            "schema_version": 2,
            "source_id": source_id,
            "coverage_id": coverage.id if coverage else None,
            "pairing_ids": [coverage.id] if coverage else [],
            "scope_id": scope.id if scope else None,
            "scope_snapshot": scope_snapshot,
        }

    with session_scope(request.app.state.engine) as session:
        job = jobs.enqueue(
            session,
            "sources.collect",
            payload=payload,
            priority=PriorityClass.INTERACTIVE,
            actor="ui",
        )
        job_id = job.id
    return _redirect(
        "/sources",
        f"Collection queued (job {job_id[:8]}) - results appear under Evidence",
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, source_id: str):
    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        source = session.scalars(
            select(SourceDefinition)
            .where(SourceDefinition.id == source_id)
            .options(
                selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets),
                selectinload(SourceDefinition.identities),
                selectinload(SourceDefinition.outbound_relationships),
                selectinload(SourceDefinition.inbound_relationships),
            )
        ).first()
        if source is None:
            return _render(
                request, "error.html", active="sources", title="Source not found", detail=source_id
            )
        documents = list(
            session.scalars(
                select(SourceDocument)
                .where(SourceDocument.source_definition_id == source_id)
                .order_by(SourceDocument.retrieved_at.desc())
                .limit(50)
            )
        )
        effective_robots_enforced = robots_enforced(source, settings)
        blockers = activation_blockers(source, enforce_robots=effective_robots_enforced)
        other_sources = list(
            session.execute(
                select(SourceDefinition.id, SourceDefinition.name)
                .where(SourceDefinition.id != source_id)
                .order_by(SourceDefinition.name)
            ).all()
        )
        session.expunge_all()
    return _render(
        request,
        "source_detail.html",
        active="sources",
        source=source,
        documents=documents,
        blockers=blockers,
        coverages=sorted(
            source.coverages,
            key=lambda coverage: (-coverage.priority, coverage.name, coverage.id),
        ),
        identities=sorted(
            source.identities,
            key=lambda identity: (identity.identity_type, identity.normalised_value),
        ),
        relationships=[
            *(("outbound", relationship) for relationship in source.outbound_relationships),
            *(("inbound", relationship) for relationship in source.inbound_relationships),
        ],
        other_sources=other_sources,
        coverage_has_distinct_endpoint=coverage_has_distinct_endpoint,
        now=utc_now(),
        lifecycle_badges=LIFECYCLE_BADGES,
        grade_badges=GRADE_BADGES,
        policy_badges=POLICY_BADGES,
        global_robots_policy=settings.robots_policy,
        effective_robots_enforced=effective_robots_enforced,
    )


@router.post("/sources/{source_id}/coverages/create")
def source_coverage_create(
    request: Request,
    source_id: str,
    coverage_key: str = Form(default=""),
    name: str = Form(default=""),
    industry_ids: str = Form(default=""),
    region_codes: str = Form(default=""),
    dimension: str = Form(default=""),
    target_key: str = Form(default=""),
    polarity: str = Form(default="include"),
    priority: int = Form(default=50),
    relevance: float = Form(default=1.0),
    confidence: float = Form(default=1.0),
    authority_tier_override: str = Form(default=""),
):
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        key = coverage_key.strip() or f"manual_{uuid.uuid4().hex[:12]}"
        targets = [
            TargetSpec("industry", value)
            for value in industry_ids.replace(";", ",").split(",")
            if value.strip()
        ]
        targets.extend(
            TargetSpec("region", value, match_mode="hierarchical")
            for value in region_codes.replace(";", ",").split(",")
            if value.strip()
        )
        if bool(dimension.strip()) != bool(target_key.strip()):
            return _redirect(
                f"/sources/{source_id}",
                "Custom dimension and target must be supplied together",
                "danger",
            )
        if dimension.strip():
            if polarity not in {"include", "exclude"}:
                return _redirect(f"/sources/{source_id}", "Invalid target polarity", "danger")
            targets.append(TargetSpec(dimension, target_key, polarity=polarity))
        try:
            tier = int(authority_tier_override) if authority_tier_override else None
            coverage, outcome = upsert_coverage(
                session,
                source,
                CoverageSpec(
                    coverage_key=key,
                    name=name.strip() or key,
                    targets=tuple(targets),
                    priority=priority,
                    relevance=relevance,
                    confidence=confidence,
                    authority_tier_override=tier,
                    origin="user",
                ),
                actor="ui",
            )
        except (CoverageValidationError, ValueError) as exc:
            return _redirect(f"/sources/{source_id}", str(exc), "danger")
        coverage_name = coverage.name
    return _redirect(f"/sources/{source_id}", f"Coverage {coverage_name}: {outcome}")


@router.post("/sources/{source_id}/coverages/{coverage_id}/disable")
def source_coverage_disable(request: Request, source_id: str, coverage_id: str):
    with session_scope(request.app.state.engine) as session:
        coverage = session.get(SourceCoverage, coverage_id)
        if coverage is None or coverage.source_definition_id != source_id:
            return _redirect(f"/sources/{source_id}", "Coverage profile not found", "danger")
        outcome = disable_coverage(
            session, coverage, actor="ui", reason="disabled in source workspace"
        )
    return _redirect(f"/sources/{source_id}", f"Coverage {outcome}", "warning")


@router.post("/sources/{source_id}/coverages/{coverage_id}/check-policy")
def source_coverage_check_policy(request: Request, source_id: str, coverage_id: str):
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        coverage = session.get(SourceCoverage, coverage_id)
        if source is None or coverage is None or coverage.source_definition_id != source_id:
            return _redirect(f"/sources/{source_id}", "Coverage profile not found", "danger")
        status = check_coverage_robots(request.app.state.settings, source, coverage)
        audit.record(
            session,
            "ui",
            "source.coverage_policy_checked",
            "source_coverage",
            coverage.id,
            {"robots_status": status},
        )
        name = coverage.name
    return _redirect(f"/sources/{source_id}", f"Robots for {name}: {status}")


@router.post("/sources/{source_id}/relationships/create")
def source_relationship_create(
    request: Request,
    source_id: str,
    related_source_id: str = Form(...),
    relationship_type: str = Form(...),
    confidence: float = Form(default=1.0),
    notes: str = Form(default=""),
):
    relationship_type = relationship_type.strip().lower().replace(" ", "_")
    if source_id == related_source_id:
        return _redirect(f"/sources/{source_id}", "A source cannot relate to itself", "danger")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", relationship_type):
        return _redirect(f"/sources/{source_id}", "Invalid relationship type", "danger")
    if not 0 <= confidence <= 1:
        return _redirect(f"/sources/{source_id}", "Confidence must be between 0 and 1", "danger")
    with session_scope(request.app.state.engine) as session:
        if session.get(SourceDefinition, source_id) is None:
            return _redirect("/sources", "Source not found", "danger")
        related = session.get(SourceDefinition, related_source_id)
        if related is None:
            return _redirect(f"/sources/{source_id}", "Related source not found", "danger")
        exists = session.scalars(
            select(SourceRelationship).where(
                SourceRelationship.source_definition_id == source_id,
                SourceRelationship.related_source_definition_id == related_source_id,
                SourceRelationship.relationship_type == relationship_type,
            )
        ).first()
        if exists:
            return _redirect(f"/sources/{source_id}", "Relationship already exists", "warning")
        relationship = SourceRelationship(
            source_definition_id=source_id,
            related_source_definition_id=related_source_id,
            relationship_type=relationship_type,
            confidence=confidence,
            origin="user",
            notes=notes.strip() or None,
        )
        session.add(relationship)
        session.flush()
        audit.record(
            session,
            "ui",
            "source.relationship_created",
            "source_relationship",
            relationship.id,
            {
                "related_source_definition_id": related_source_id,
                "relationship_type": relationship_type,
            },
        )
        related_name = related.name
    return _redirect(f"/sources/{source_id}", f"Added {relationship_type} link to {related_name}")


# --- Evidence --------------------------------------------------------------------


@router.post("/sources/{source_id}/evidence/upload")
def evidence_upload(
    request: Request,
    source_id: str,
    file: Annotated[UploadFile, File()],
):
    from heatseeker_source_registry.manual_evidence import add_manual_file

    limit = request.app.state.settings.evidence_upload_max_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := file.file.read(min(1024 * 1024, limit - total + 1)):
        total += len(chunk)
        if total > limit:
            return _redirect(
                f"/sources/{source_id}",
                f"Upload exceeds {limit:,} bytes",
                "danger",
            )
        chunks.append(chunk)
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return _redirect("/sources", "Source not found", "danger")
        try:
            document, created = add_manual_file(
                session,
                request.app.state.settings,
                source,
                b"".join(chunks),
                filename=file.filename or "evidence.bin",
                content_type=file.content_type,
                actor="ui",
            )
        except ValueError as exc:
            return _redirect(f"/sources/{source_id}", str(exc), "danger")
        document_id = document.id
    message = "Evidence uploaded and processing queued" if created else "Evidence already existed"
    return _redirect(f"/evidence/{document_id}", message)


@router.get("/evidence", response_class=HTMLResponse)
def evidence_page(request: Request):
    from heatseeker_source_registry.document_pipeline import latest_processing_run

    with session_scope(request.app.state.engine) as session:
        rows = list(
            session.execute(
                select(SourceDocument, SourceDefinition.name)
                .join(
                    SourceDefinition,
                    SourceDefinition.id == SourceDocument.source_definition_id,
                )
                .order_by(SourceDocument.retrieved_at.desc())
                .limit(100)
            ).all()
        )
        documents = [
            {
                "doc": doc,
                "source_name": name,
                "processing": latest_processing_run(session, doc.id),
            }
            for doc, name in rows
        ]
        session.expunge_all()
    return _render(
        request,
        "evidence.html",
        active="evidence",
        documents=documents,
        ocr_status=(
            "unavailable" if request.app.state.settings.evidence_ocr_enabled else "disabled"
        ),
        vision_status=(
            "unavailable" if request.app.state.settings.evidence_vision_enabled else "disabled"
        ),
    )


@router.get("/evidence/{document_id}", response_class=HTMLResponse)
def evidence_detail(request: Request, document_id: str):
    from heatseeker_source_registry.document_pipeline import latest_processing_run, read_run_text
    from heatseeker_source_registry.models import DocumentProcessingRun, SourceDocumentReference

    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            return _render(
                request,
                "error.html",
                active="evidence",
                title="Document not found",
                detail=document_id,
            )
        source = session.get(SourceDefinition, document.source_definition_id)
        source_name = source.name if source else "?"
        processing = latest_processing_run(session, document.id)
        processing_runs = list(
            session.scalars(
                select(DocumentProcessingRun)
                .where(DocumentProcessingRun.source_document_id == document.id)
                .order_by(DocumentProcessingRun.created_at.desc())
            )
        )
        references = list(
            session.scalars(
                select(SourceDocumentReference)
                .where(SourceDocumentReference.parent_document_id == document.id)
                .order_by(SourceDocumentReference.ordinal)
            )
        )
        try:
            extracted_text = read_run_text(settings, processing) if processing else None
        except FileNotFoundError:
            extracted_text = None
        session.expunge_all()

    preview = html.escape(extracted_text[:40_000]) if extracted_text else None
    content_type = (document.detected_content_type or document.content_type or "").lower()
    is_image = content_type.split(";", 1)[0] in {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
    return _render(
        request,
        "evidence_detail.html",
        active="evidence",
        doc=document,
        source_name=source_name,
        preview=preview,
        processing=processing,
        processing_runs=processing_runs,
        references=references,
        is_image=is_image,
    )


@router.post("/evidence/{document_id}/process")
def evidence_process(request: Request, document_id: str):
    from heatseeker_source_registry.document_pipeline import enqueue_document_processing

    with session_scope(request.app.state.engine) as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            return _redirect("/evidence", "Document not found", "danger")
        job = enqueue_document_processing(
            session,
            request.app.state.settings,
            document,
            actor="ui",
            priority=PriorityClass.INTERACTIVE,
        )
    message = "Document processing queued" if job else "Current processing result already exists"
    return _redirect(f"/evidence/{document_id}", message)


# --- Research scopes ----------------------------------------------------------------


@router.get("/scopes", response_class=HTMLResponse)
def scopes_page(request: Request):
    with session_scope(request.app.state.engine) as session:
        ensure_default_scopes(session)
        load_regions(session)  # seeds builtin regions and refreshes the registry
        scopes = list(session.scalars(select(ResearchScope).order_by(ResearchScope.name)))
        regions = list(session.scalars(select(GeoRegion).order_by(GeoRegion.code)))
        session.expunge_all()
    return _render(
        request,
        "scopes.html",
        active="scopes",
        scopes=scopes,
        regions=regions,
        known_codes=KNOWN_CODES,
        describe=describe,
        industry_choices=[path.name for path in discover_packs()],
    )


@router.post("/scopes/create")
def scopes_create(
    request: Request,
    name: str = Form(...),
    codes: str = Form(default=""),
    exclude: str = Form(default=""),
    industries: str = Form(default=""),
    target_filters: str = Form(default=""),
    include_unknown: bool = Form(default=False),
    description: str = Form(default=""),
):
    if not name.strip():
        return _redirect("/scopes", "Name is required", "danger")
    try:
        filters = json.loads(target_filters) if target_filters.strip() else {}
        if not isinstance(filters, dict):
            raise ValueError("target filters must be a JSON object")
    except (json.JSONDecodeError, ValueError) as exc:
        return _redirect("/scopes", f"Invalid target filters: {exc}", "danger")
    with session_scope(request.app.state.engine) as session:
        existing = session.scalars(
            select(ResearchScope).where(ResearchScope.name == name.strip())
        ).first()
        if existing:
            return _redirect("/scopes", f"A scope named '{name}' already exists", "warning")
        try:
            scope = create_scope(
                session,
                name,
                codes,
                description or None,
                actor="ui",
                industry_ids_raw=industries,
                target_filters=filters,
                include_unknown=include_unknown,
                exclude_raw=exclude,
            )
        except ValueError as exc:
            return _redirect("/scopes", str(exc), "danger")
        scope_name = scope.name
    return _redirect("/scopes", f"Created scope {scope_name}")


@router.post("/scopes/{scope_id}/activate")
def scopes_activate(request: Request, scope_id: str):
    with session_scope(request.app.state.engine) as session:
        scope = set_active(session, scope_id, actor="ui")
        if scope is None:
            return _redirect("/scopes", "Scope not found", "danger")
        name, codes = scope.name, ", ".join(scope.geo_codes)
    return _redirect("/scopes", f"Active scope: {name} ({codes})")


@router.post("/scopes/regions/save")
def scopes_region_save(
    request: Request,
    code: str = Form(...),
    name: str = Form(default=""),
    members: str = Form(default=""),
):
    member_codes = [m for m in members.replace(";", ",").split(",") if m.strip()]
    with session_scope(request.app.state.engine) as session:
        try:
            region = upsert_region(session, code, name, member_codes, actor="ui")
        except ValueError as exc:
            return _redirect("/scopes", str(exc), "danger")
        saved = f"{region.code} ({len(region.member_codes)} members)"
    return _redirect("/scopes", f"Saved region {saved}")


@router.post("/scopes/regions/{code}/delete")
def scopes_region_delete(request: Request, code: str):
    with session_scope(request.app.state.engine) as session:
        try:
            delete_region(session, code, actor="ui")
        except ValueError as exc:
            return _redirect("/scopes", str(exc), "danger")
    return _redirect("/scopes", f"Deleted region {code.upper()}")
