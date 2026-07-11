"""JSON API under /api/* (ADR-0009). UI pages live in ui_routes.

The source endpoints intentionally keep the original, flat ``GET /api/sources``
response while exposing association-aware coverage profiles additively.  A coverage is
the unit that correlates an industry, a region, and any additional targeting dimensions;
filters must never be satisfied by targets from two different coverages.
"""

import re
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from heatseeker_common import audit
from heatseeker_common import jobs as job_queue
from heatseeker_common.db import session_scope
from heatseeker_common.health import check_health
from heatseeker_common.models import Job, PriorityClass
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    GeographyMatchMode,
    match_geography,
    normalise_codes,
    validate_code,
)
from heatseeker_industry_packs.loader import PackValidationError, discover_packs, load_pack
from heatseeker_industry_packs.models import PackRegistration
from heatseeker_source_registry.identity import (
    attach_identity,
    resolve_identities,
    url_identity,
)
from heatseeker_source_registry.models import (
    ResearchScope,
    RobotsStatus,
    SourceCoverage,
    SourceDefinition,
    SourceRelationship,
)
from heatseeker_source_registry.policy import (
    activation_blockers,
    check_coverage_robots,
    robots_enforced,
)
from heatseeker_source_registry.scopes import active_scope, create_scope, set_active
from heatseeker_source_registry.targeting import (
    CoverageSpec,
    TargetSpec,
    disable_coverage,
    match_coverages,
    normalise_dimension,
    normalise_target_key,
    serialize_coverage,
    upsert_coverage,
    validate_collection_scope,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/api", tags=["api"])

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")


def _clean_slug(value: str, *, field_name: str) -> str:
    value = value.strip().lower().replace(" ", "_")
    if not value or not _SLUG_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase identifier")
    return value


class SourceCreateRequest(BaseModel):
    """Validated boundary for manually registered canonical sources."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    source_category: str = Field(min_length=1, max_length=50)
    base_url: str | None = Field(default=None, max_length=1000)
    jurisdiction: str | None = Field(default=None, max_length=100)
    geo_codes: list[str] | None = None
    access_method: Literal["api", "bulk", "rss", "sitemap", "html", "rendered", "manual"] = "html"
    authority_tier: int = Field(default=5, ge=1, le=7)
    language: str | None = Field(default=None, max_length=35)
    expected_update_frequency: str | None = Field(default=None, max_length=100)
    authentication_type: str | None = Field(default=None, max_length=50)
    rate_limit_policy: dict | None = None
    collection_scope: dict | None = None
    parser_profile: str | None = Field(default=None, max_length=200)
    respect_robots_override: bool | None = None
    notes: str | None = Field(default=None, max_length=20_000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("source_category")
    @classmethod
    def _category_slug(cls, value: str) -> str:
        return _clean_slug(value, field_name="source_category")

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials")
        return value

    @field_validator("geo_codes")
    @classmethod
    def _normalise_geo_codes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalise_codes(value, validate=True)

    _collection_scope_ok = field_validator("collection_scope")(validate_collection_scope)

    @model_validator(mode="after")
    def _automated_source_has_url(self):
        if self.access_method != "manual" and not self.base_url:
            raise ValueError("base_url is required unless access_method is manual")
        return self


class SourcePatchRequest(BaseModel):
    """Partial canonical metadata update; policy/lifecycle remain separate workflows."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=300)
    source_category: str | None = Field(default=None, min_length=1, max_length=50)
    base_url: str | None = Field(default=None, max_length=1000)
    jurisdiction: str | None = Field(default=None, max_length=100)
    geo_codes: list[str] | None = None
    access_method: Literal["api", "bulk", "rss", "sitemap", "html", "rendered", "manual"] | None = (
        None
    )
    authority_tier: int | None = Field(default=None, ge=1, le=7)
    language: str | None = Field(default=None, max_length=35)
    expected_update_frequency: str | None = Field(default=None, max_length=100)
    authentication_type: str | None = Field(default=None, max_length=50)
    rate_limit_policy: dict | None = None
    collection_scope: dict | None = None
    parser_profile: str | None = Field(default=None, max_length=200)
    respect_robots_override: bool | None = None
    notes: str | None = Field(default=None, max_length=20_000)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("source_category")
    @classmethod
    def _category_slug(cls, value: str | None) -> str | None:
        return _clean_slug(value, field_name="source_category") if value else None

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials")
        return value

    @field_validator("geo_codes")
    @classmethod
    def _normalise_geo_codes(cls, value: list[str] | None) -> list[str] | None:
        return normalise_codes(value, validate=True) if value is not None else None

    _collection_scope_ok = field_validator("collection_scope")(validate_collection_scope)


class CoverageTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1, max_length=50)
    target_key: str | None = Field(default=None, max_length=300)
    target: str | None = Field(default=None, max_length=300)
    target_label: str | None = Field(default=None, max_length=300)
    polarity: Literal["include", "exclude"] = "include"
    match_mode: Literal["exact", "hierarchical", "covers", "within"] | None = None

    @field_validator("dimension")
    @classmethod
    def _dimension_slug(cls, value: str) -> str:
        return _clean_slug(value, field_name="dimension")

    @model_validator(mode="after")
    def _one_target_key(self):
        if self.target_key and self.target and self.target_key.strip() != self.target.strip():
            raise ValueError("target and target_key disagree")
        raw = self.target_key or self.target
        if not raw or not raw.strip():
            raise ValueError("target_key is required")
        normalised = TargetSpec(
            self.dimension,
            raw,
            self.target_label,
            self.polarity,
            self.match_mode,
        ).normalised()
        self.dimension = normalised.dimension
        self.target_key = normalised.target_key
        self.target_label = normalised.target_label
        self.match_mode = normalised.match_mode
        return self


class CoverageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_key: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    priority: int = Field(default=50, ge=0, le=100)
    relevance: float = Field(default=1.0, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    authority_tier_override: int | None = Field(default=None, ge=1, le=7)
    collection_scope_override: dict | None = None
    parser_profile_override: str | None = Field(default=None, max_length=200)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    industry_ids: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list)
    targets: list[CoverageTargetRequest] = Field(default_factory=list)

    @field_validator("coverage_key")
    @classmethod
    def _coverage_key_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_slug(value, field_name="coverage_key")

    @field_validator("industry_ids")
    @classmethod
    def _industry_ids(cls, values: list[str]) -> list[str]:
        return sorted({_clean_slug(value, field_name="industry_id") for value in values})

    @field_validator("region_codes")
    @classmethod
    def _region_codes(cls, values: list[str]) -> list[str]:
        return normalise_codes(values, validate=True)

    @model_validator(mode="after")
    def _validity_order(self):
        for label, value in (("valid_from", self.valid_from), ("valid_to", self.valid_to)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{label} must include a timezone")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        return self


class CoveragePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    lifecycle_status: Literal["active", "disabled"] | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    relevance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    authority_tier_override: int | None = Field(default=None, ge=1, le=7)
    collection_scope_override: dict | None = None
    parser_profile_override: str | None = Field(default=None, max_length=200)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    industry_ids: list[str] | None = None
    region_codes: list[str] | None = None
    targets: list[CoverageTargetRequest] | None = None

    @field_validator("industry_ids")
    @classmethod
    def _industry_ids(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return sorted({_clean_slug(value, field_name="industry_id") for value in values})

    @field_validator("region_codes")
    @classmethod
    def _region_codes(cls, values: list[str] | None) -> list[str] | None:
        return normalise_codes(values, validate=True) if values is not None else None

    @model_validator(mode="after")
    def _validity_order(self):
        for label, value in (("valid_from", self.valid_from), ("valid_to", self.valid_to)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{label} must include a timezone")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        return self


class SourceRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    related_source_definition_id: str
    relationship_type: str = Field(min_length=1, max_length=50)
    confidence: float = Field(default=1.0, ge=0, le=1)
    notes: str | None = Field(default=None, max_length=20_000)

    @field_validator("relationship_type")
    @classmethod
    def _relationship_slug(cls, value: str) -> str:
        return _clean_slug(value, field_name="relationship_type")


class SourceCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_id: str | None = None
    scope_id: str | None = None


class ScopeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=20_000)
    geo_codes: list[str] = Field(default_factory=list)
    exclude_codes: list[str] = Field(default_factory=list)
    industry_ids: list[str] = Field(default_factory=list)
    target_filters: dict = Field(default_factory=dict)
    include_unknown: bool = True

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("scope name must not be blank")
        return value


class RegionUpsertRequest(BaseModel):
    """Create or redefine a named geography region (regions are data, ADR-0012)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=3, max_length=50)
    name: str = Field(default="", max_length=200)
    member_codes: list[str] = Field(min_length=1)


def _source_models():
    """Keep coverage-model imports local to the source API integration boundary."""
    from heatseeker_source_registry.models import (
        SourceCoverage,
        SourceCoverageTarget,
        SourceDefinition,
    )

    return SourceDefinition, SourceCoverage, SourceCoverageTarget


def _target_key(target) -> str:
    return str(target.target_key)


def coverage_to_dict(coverage) -> dict:
    targets = sorted(
        coverage.targets,
        key=lambda target: (target.dimension, target.polarity, _target_key(target)),
    )
    target_rows = [
        {
            "id": target.id,
            "dimension": target.dimension,
            "target_key": _target_key(target),
            "target": _target_key(target),  # ergonomic alias for generic clients
            "target_label": target.target_label,
            "polarity": target.polarity,
            "match_mode": target.match_mode,
        }
        for target in targets
    ]
    return {
        "id": coverage.id,
        "source_definition_id": coverage.source_definition_id,
        "coverage_key": coverage.coverage_key,
        "name": coverage.name,
        "description": coverage.description,
        "lifecycle_status": coverage.lifecycle_status,
        "priority": coverage.priority,
        "relevance": coverage.relevance,
        "confidence": coverage.confidence,
        "authority_tier_override": coverage.authority_tier_override,
        "collection_scope_override": coverage.collection_scope_override,
        "parser_profile_override": coverage.parser_profile_override,
        "robots_status": coverage.robots_status,
        "robots_checked_at": coverage.robots_checked_at,
        "origin": coverage.origin,
        "origin_pack_id": coverage.origin_pack_id,
        "origin_pack_version": coverage.origin_pack_version,
        "origin_pack_hash": coverage.origin_pack_hash,
        "valid_from": coverage.valid_from,
        "valid_to": coverage.valid_to,
        "created_at": coverage.created_at,
        "updated_at": coverage.updated_at,
        "targets": target_rows,
        "industry_ids": sorted(
            {
                row["target_key"]
                for row in target_rows
                if row["dimension"] == "industry" and row["polarity"] == "include"
            }
        ),
        "region_codes": sorted(
            {
                row["target_key"]
                for row in target_rows
                if row["dimension"] == "region" and row["polarity"] == "include"
            }
        ),
    }


def _load_coverages_by_source(session, source_ids: list[str]) -> dict[str, list]:
    if not source_ids:
        return {}
    _, SourceCoverage, _ = _source_models()
    rows = list(
        session.scalars(
            select(SourceCoverage)
            .where(SourceCoverage.source_definition_id.in_(source_ids))
            .options(selectinload(SourceCoverage.targets))
            .order_by(SourceCoverage.priority.desc(), SourceCoverage.name, SourceCoverage.id)
        )
    )
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row.source_definition_id].append(row)
    return grouped


def _keys_match(target_row: dict, wanted: str) -> bool:
    actual = target_row["target_key"]
    if target_row["dimension"] == "region":
        mode = {
            "hierarchical": GeographyMatchMode.OVERLAPS,
            "covers": GeographyMatchMode.COVERS,
            "within": GeographyMatchMode.WITHIN,
            "exact": GeographyMatchMode.EXACT,
        }[target_row["match_mode"]]
        return match_geography([actual], [wanted], mode=mode, include_unknown=False)
    return actual.casefold() == wanted.casefold()


def coverage_matches(
    coverage: dict,
    selectors: list[tuple[str, str]],
    *,
    include_unknown: bool = False,
) -> bool:
    """Match every selector inside this one coverage (never across coverages)."""
    if coverage["lifecycle_status"] != "active":
        return False
    now = utc_now()
    if coverage["valid_from"] and coverage["valid_from"] > now:
        return False
    if coverage["valid_to"] and coverage["valid_to"] < now:
        return False
    targets: list[dict] = coverage["targets"]
    for dimension, wanted in selectors:
        dimension_targets = [row for row in targets if row["dimension"] == dimension]
        excludes = [row for row in dimension_targets if row["polarity"] == "exclude"]
        if any(_keys_match(row, wanted) for row in excludes):
            return False
        includes = [row for row in dimension_targets if row["polarity"] == "include"]
        if not includes:
            if include_unknown:
                continue
            return False
        if not any(_keys_match(row, wanted) for row in includes):
            return False
    return True


def _selectors(
    industry_id: str | None,
    region_code: str | None,
    dimension: str | None,
    target: str | None,
) -> list[tuple[str, str]]:
    if bool(dimension) != bool(target):
        raise HTTPException(
            status_code=400,
            detail="dimension and target must be supplied together",
        )
    selectors: list[tuple[str, str]] = []
    if industry_id:
        try:
            selectors.append(("industry", normalise_target_key("industry", industry_id)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if region_code:
        try:
            selectors.append(("region", validate_code(region_code)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dimension and target:
        try:
            dimension = normalise_dimension(dimension)
            target = normalise_target_key(dimension, target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        selectors.append((dimension, target))
    return selectors


def source_to_dict(
    source,
    coverages: list,
    *,
    selectors: list[tuple[str, str]] | None = None,
    include_pairings: bool = False,
    include_unknown: bool = False,
) -> dict:
    coverage_dicts = [coverage_to_dict(row) for row in coverages]
    now = utc_now()
    active = [
        row
        for row in coverage_dicts
        if row["lifecycle_status"] == "active"
        and (row["valid_from"] is None or row["valid_from"] <= now)
        and (row["valid_to"] is None or row["valid_to"] >= now)
    ]
    selectors = selectors or []
    matching = [
        row for row in active if coverage_matches(row, selectors, include_unknown=include_unknown)
    ]
    industry_ids = sorted({key for row in active for key in row["industry_ids"]})
    region_codes = sorted({key for row in active for key in row["region_codes"]})
    if not coverage_dicts:
        # Legacy summaries remain informative during migration/backfill, but never pretend
        # to be a coherent pairing for association-aware filtering.
        industry_ids = [source.pack_id] if source.pack_id else []
        region_codes = sorted(source.geo_codes or [])
    result = {
        # Original response keys -- do not rename or remove.
        "id": source.id,
        "name": source.name,
        "category": source.source_category,
        "base_url": source.base_url,
        "jurisdiction": source.jurisdiction,
        "geo_codes": source.geo_codes,
        "access_method": source.access_method,
        "authority_tier": source.authority_tier,
        "language": source.language,
        "expected_update_frequency": source.expected_update_frequency,
        "authentication_type": source.authentication_type,
        "rate_limit_policy": source.rate_limit_policy,
        "parser_profile": source.parser_profile,
        "lifecycle_status": source.lifecycle_status,
        "robots_status": source.robots_status,
        "respect_robots_override": source.respect_robots_override,
        "terms_status": source.terms_status,
        "last_success_at": source.last_success_at,
        "consecutive_failures": source.consecutive_failures,
        # Association-aware additive fields.
        "source_category": source.source_category,
        "pack_id": source.pack_id,
        "origin": source.origin,
        "health_score": source.health_score,
        "industry_ids": industry_ids,
        "region_codes": region_codes,
        "pairing_count": len(active),
        "coverage_count": len(coverage_dicts),
        "matching_pairing_count": len(matching),
        "has_matching_pairing": bool(matching),
        "targeting_state": (
            "unpaired"
            if not coverage_dicts
            else "unknown"
            if any(not row["targets"] for row in active)
            else "targeted"
        ),
    }
    if include_pairings:
        result["pairings"] = coverage_dicts
        result["coverages"] = coverage_dicts
    return result


def _requests_to_target_values(payload) -> list[dict]:
    rows: list[dict] = []
    for industry_id in payload.industry_ids or []:
        key = industry_id.strip()
        if key:
            rows.append(
                {
                    "dimension": "industry",
                    "target_key": key,
                    "polarity": "include",
                    "match_mode": "exact",
                }
            )
    for region_code in payload.region_codes or []:
        if region_code.strip():
            rows.append(
                {
                    "dimension": "region",
                    "target_key": validate_code(region_code),
                    "polarity": "include",
                    "match_mode": "hierarchical",
                }
            )
    for target in payload.targets or []:
        key = target.target_key.strip()
        if target.dimension == "region":
            key = validate_code(key)
        rows.append(
            {
                "dimension": target.dimension,
                "target_key": key,
                "target_label": target.target_label,
                "polarity": target.polarity,
                "match_mode": target.match_mode,
            }
        )
    unique: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        unique[(row["dimension"], row["target_key"], row["polarity"])] = row
    return list(unique.values())


def _replace_coverage_targets(session, coverage, target_values: list[dict]) -> None:
    _, _, SourceCoverageTarget = _source_models()
    for existing in list(coverage.targets):
        session.delete(existing)
    session.flush()
    for values in target_values:
        session.add(SourceCoverageTarget(source_coverage_id=coverage.id, **values))
    session.flush()


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "payload": job.payload,
        "result": job.result,
        "error": job.error,
        "run_at": job.run_at,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "claimed_by": job.claimed_by,
        "correlation_id": job.correlation_id,
        "cancel_requested": job.cancel_requested,
    }


@router.get("/health")
def api_health(request: Request) -> JSONResponse:
    report = check_health(request.app.state.engine, request.app.state.settings)
    return JSONResponse(report, status_code=200 if report["status"] == "ok" else 503)


@router.get("/jobs")
def api_list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Job.status == status)
    with session_scope(request.app.state.engine) as session:
        return [job_to_dict(job) for job in session.scalars(stmt)]


@router.get("/jobs/{job_id}")
def api_get_job(request: Request, job_id: str) -> dict:
    with session_scope(request.app.state.engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job_to_dict(job)


def _source_stmt(source_status: str | None, category: str | None):
    stmt = (
        select(SourceDefinition)
        .options(
            selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets),
            selectinload(SourceDefinition.identities),
        )
        .order_by(SourceDefinition.authority_tier, SourceDefinition.name, SourceDefinition.id)
    )
    if source_status:
        stmt = stmt.where(SourceDefinition.lifecycle_status == source_status)
    if category:
        stmt = stmt.where(SourceDefinition.source_category == category)
    return stmt


def _list_sources(
    session,
    *,
    source_status: str | None,
    category: str | None,
    selectors: list[tuple[str, str]],
    include_pairings: bool,
    include_unknown: bool,
    unpaired: bool,
    limit: int,
) -> list[dict]:
    sources = list(session.scalars(_source_stmt(source_status, category)))
    rows = [
        source_to_dict(
            source,
            source.coverages,
            selectors=selectors,
            include_pairings=include_pairings,
            include_unknown=include_unknown,
        )
        for source in sources
    ]
    if selectors:
        rows = [row for row in rows if row["has_matching_pairing"]]
    if unpaired:
        rows = [row for row in rows if row["pairing_count"] == 0]
    return rows[:limit]


@router.get("/sources")
def api_list_sources(
    request: Request,
    status: str | None = Query(default=None),
    category: str | None = Query(default=None),
    industry_id: str | None = Query(default=None),
    region_code: str | None = Query(default=None),
    dimension: str | None = Query(default=None),
    target: str | None = Query(default=None),
    include: str = Query(default=""),
    include_unknown: bool = Query(default=False),
    unpaired: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=500),
) -> list[dict]:
    """List sources; multi-dimensional filters resolve within one coverage tuple."""
    selectors = _selectors(industry_id, region_code, dimension, target)
    include_pairings = bool({"pairings", "coverages"} & set(include.split(",")))
    with session_scope(request.app.state.engine) as session:
        return _list_sources(
            session,
            source_status=status,
            category=category,
            selectors=selectors,
            include_pairings=include_pairings,
            include_unknown=include_unknown,
            unpaired=unpaired,
            limit=limit,
        )


@router.get("/sources/resolve")
def api_resolve_sources(
    request: Request,
    industry_id: str | None = Query(default=None),
    region_code: str | None = Query(default=None),
    dimension: str | None = Query(default=None),
    target: str | None = Query(default=None),
    include_unknown: bool = Query(default=False),
    status: str | None = Query(default="active"),
    limit: int = Query(default=500, ge=1, le=500),
) -> list[dict]:
    """Resolve eligible sources with deterministic per-coverage match explanations."""
    selectors = _selectors(industry_id, region_code, dimension, target)
    if not selectors:
        raise HTTPException(status_code=400, detail="at least one target selector is required")
    with session_scope(request.app.state.engine) as session:
        sources = list(session.scalars(_source_stmt(status, None)))
        rows = []
        for source in sources:
            industries = [value for key, value in selectors if key == "industry"]
            regions = [value for key, value in selectors if key == "region"]
            filters: dict[str, list[str]] = defaultdict(list)
            for key, value in selectors:
                if key not in {"industry", "region"}:
                    filters[key].append(value)
            explanation = match_coverages(
                source.coverages,
                industry_ids=industries,
                region_codes=regions,
                target_filters=filters,
                include_unknown=include_unknown,
            )
            if explanation:
                row = source_to_dict(
                    source,
                    source.coverages,
                    selectors=selectors,
                    include_pairings=True,
                    include_unknown=include_unknown,
                )
                row["match"] = explanation.as_dict()
                rows.append(row)
                if len(rows) >= limit:
                    break
        return rows


@router.post("/sources", status_code=201)
def api_create_source(request: Request, payload: SourceCreateRequest) -> dict:
    with session_scope(request.app.state.engine) as session:
        identity = url_identity(payload.base_url) if payload.base_url else None
        if identity and resolve_identities(session, [identity]) is not None:
            raise HTTPException(status_code=409, detail="a source with this URL already exists")
        source = SourceDefinition(
            name=payload.name,
            source_category=payload.source_category,
            base_url=payload.base_url,
            jurisdiction=payload.jurisdiction,
            geo_codes=payload.geo_codes,
            language=payload.language,
            access_method=payload.access_method,
            authority_tier=payload.authority_tier,
            expected_update_frequency=payload.expected_update_frequency,
            authentication_type=payload.authentication_type,
            rate_limit_policy=payload.rate_limit_policy,
            collection_scope=payload.collection_scope,
            parser_profile=payload.parser_profile,
            respect_robots_override=payload.respect_robots_override,
            origin="user",
            notes=payload.notes,
        )
        session.add(source)
        session.flush()
        if identity:
            attach_identity(session, source, identity, origin="user", is_primary=True)
        audit.record(
            session,
            "api",
            "source.created",
            "source",
            source.id,
            {"name": source.name, "base_url": source.base_url},
        )
        return source_to_dict(source, [], include_pairings=True)


@router.get("/sources/{source_id}")
def api_get_source(request: Request, source_id: str) -> dict:
    stmt = (
        select(SourceDefinition)
        .where(SourceDefinition.id == source_id)
        .options(
            selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets),
            selectinload(SourceDefinition.identities),
            selectinload(SourceDefinition.outbound_relationships),
            selectinload(SourceDefinition.inbound_relationships),
        )
    )
    with session_scope(request.app.state.engine) as session:
        source = session.scalars(stmt).first()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        result = source_to_dict(source, source.coverages, include_pairings=True)
        result.update(
            language=source.language,
            expected_update_frequency=source.expected_update_frequency,
            authentication_type=source.authentication_type,
            rate_limit_policy=source.rate_limit_policy,
            collection_scope=source.collection_scope,
            parser_profile=source.parser_profile,
            notes=source.notes,
            identities=[
                {
                    "id": identity.id,
                    "type": identity.identity_type,
                    "value": identity.identity_value,
                    "normalised_value": identity.normalised_value,
                    "is_primary": identity.is_primary,
                    "origin": identity.origin,
                }
                for identity in sorted(
                    source.identities,
                    key=lambda item: (item.identity_type, item.normalised_value),
                )
            ],
            relationships=[
                {
                    "id": relationship.id,
                    "direction": direction,
                    "source_definition_id": relationship.source_definition_id,
                    "related_source_definition_id": relationship.related_source_definition_id,
                    "relationship_type": relationship.relationship_type,
                    "confidence": relationship.confidence,
                    "origin": relationship.origin,
                    "notes": relationship.notes,
                }
                for direction, relationship in sorted(
                    [
                        *(("outbound", item) for item in source.outbound_relationships),
                        *(("inbound", item) for item in source.inbound_relationships),
                    ],
                    key=lambda pair: (
                        pair[1].relationship_type,
                        pair[0],
                        pair[1].related_source_definition_id,
                    ),
                )
            ],
        )
        return result


@router.patch("/sources/{source_id}")
def api_patch_source(request: Request, source_id: str, payload: SourcePatchRequest) -> dict:
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="no source fields supplied")
    required_fields = {"name", "source_category", "access_method", "authority_tier"}
    cleared_required = {
        field_name
        for field_name in required_fields & payload.model_fields_set
        if getattr(payload, field_name) is None
    }
    if cleared_required:
        raise HTTPException(
            status_code=400,
            detail="required fields cannot be null: " + ", ".join(sorted(cleared_required)),
        )
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        old_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
        old_access_method = source.access_method
        access_method = (
            payload.access_method
            if "access_method" in payload.model_fields_set
            else source.access_method
        )
        base_url = payload.base_url if "base_url" in payload.model_fields_set else source.base_url
        if access_method != "manual" and not base_url:
            raise HTTPException(
                status_code=400, detail="base_url is required unless access_method is manual"
            )
        if "base_url" in payload.model_fields_set and base_url:
            identity = url_identity(base_url)
            resolved = resolve_identities(session, [identity])
            if resolved is not None and resolved.id != source.id:
                raise HTTPException(status_code=409, detail="URL belongs to another source")
            attach_identity(
                session,
                source,
                identity,
                origin="user",
                is_primary=not source.identities,
            )

        mutable_fields = (
            "name",
            "source_category",
            "base_url",
            "jurisdiction",
            "geo_codes",
            "access_method",
            "authority_tier",
            "language",
            "expected_update_frequency",
            "authentication_type",
            "rate_limit_policy",
            "collection_scope",
            "parser_profile",
            "respect_robots_override",
            "notes",
        )
        changed_fields = []
        for field_name in mutable_fields:
            if field_name not in payload.model_fields_set:
                continue
            value = getattr(payload, field_name)
            if getattr(source, field_name) != value:
                setattr(source, field_name, value)
                changed_fields.append(field_name)
        new_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
        if old_policy_url != new_policy_url:
            source.robots_status = RobotsStatus.UNKNOWN
            source.robots_checked_at = None
        if old_access_method != source.access_method:
            source.robots_status = RobotsStatus.UNKNOWN
            source.robots_checked_at = None
            for coverage in source.coverages:
                coverage.robots_status = RobotsStatus.UNKNOWN
                coverage.robots_checked_at = None
                coverage.updated_at = utc_now()
        if changed_fields:
            source.updated_at = utc_now()
            audit.record(
                session,
                "api",
                "source.updated",
                "source",
                source.id,
                {"changed_fields": changed_fields},
            )
        coverages = list(source.coverages)
        for coverage in coverages:
            _ = coverage.targets
        return source_to_dict(source, coverages, include_pairings=True)


@router.post("/sources/{source_id}/activate")
def api_activate_source(request: Request, source_id: str) -> dict:
    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        blockers = activation_blockers(
            source,
            enforce_robots=robots_enforced(source, settings),
        )
        if blockers:
            raise HTTPException(status_code=409, detail="; ".join(blockers))
        source.lifecycle_status = "active"
        source.updated_at = utc_now()
        audit.record(
            session,
            "api",
            "source.activated",
            "source",
            source.id,
            {"robots_enforced": robots_enforced(source, settings)},
        )
        return source_to_dict(source, list(source.coverages), include_pairings=True)


@router.post("/sources/{source_id}/collect", status_code=202)
def api_collect_source(request: Request, source_id: str, payload: SourceCollectRequest) -> dict:
    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        if source.lifecycle_status not in {"active", "degraded"}:
            raise HTTPException(
                status_code=409,
                detail=f"source is {source.lifecycle_status}, not collectable",
            )
        coverage = session.get(SourceCoverage, payload.coverage_id) if payload.coverage_id else None
        if payload.coverage_id and (coverage is None or coverage.source_definition_id != source_id):
            raise HTTPException(status_code=404, detail="source coverage not found")
        blockers = activation_blockers(
            source,
            coverage,
            enforce_robots=robots_enforced(source, settings),
        )
        if blockers:
            raise HTTPException(status_code=409, detail="; ".join(blockers))
        scope = (
            session.get(ResearchScope, payload.scope_id)
            if payload.scope_id
            else active_scope(session)
        )
        if payload.scope_id and scope is None:
            raise HTTPException(status_code=404, detail="research scope not found")
        if coverage and scope:
            compatible = match_coverages(
                [coverage],
                industry_ids=scope.industry_ids,
                region_codes=scope.geo_codes,
                target_filters=scope.target_filters,
                include_unknown=scope.include_unknown,
            )
            if not compatible:
                raise HTTPException(
                    status_code=409,
                    detail="coverage does not match the selected research scope",
                )
        scope_snapshot = _scope_to_dict(scope) if scope else None
        job = job_queue.enqueue(
            session,
            "sources.collect",
            payload={
                "schema_version": 2,
                "source_id": source.id,
                "coverage_id": coverage.id if coverage else None,
                "pairing_ids": [coverage.id] if coverage else [],
                "scope_id": scope.id if scope else None,
                "scope_snapshot": scope_snapshot,
            },
            priority=PriorityClass.INTERACTIVE,
            actor="api",
        )
        return {"job_id": job.id, "status": job.status, "payload": job.payload}


def _coverage_request_spec(
    payload: CoverageCreateRequest,
    *,
    coverage_key: str,
    origin: str = "user",
) -> CoverageSpec:
    return CoverageSpec(
        coverage_key=coverage_key,
        name=payload.name,
        description=payload.description,
        priority=payload.priority,
        relevance=payload.relevance,
        confidence=payload.confidence,
        authority_tier_override=payload.authority_tier_override,
        collection_scope_override=payload.collection_scope_override,
        parser_profile_override=payload.parser_profile_override,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
        origin=origin,
        targets=tuple(TargetSpec(**target) for target in _requests_to_target_values(payload)),
    )


@router.post("/sources/{source_id}/coverages", status_code=201)
def api_create_source_coverage(
    request: Request, source_id: str, payload: CoverageCreateRequest
) -> dict:
    with session_scope(request.app.state.engine) as session:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        coverage_key = payload.coverage_key or f"manual_{uuid.uuid4().hex[:12]}"
        exists = session.scalars(
            select(SourceCoverage).where(
                SourceCoverage.source_definition_id == source_id,
                SourceCoverage.coverage_key == coverage_key,
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="coverage key already exists")
        try:
            coverage, _ = upsert_coverage(
                session,
                source,
                _coverage_request_spec(payload, coverage_key=coverage_key),
                actor="api",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.refresh(coverage, ["targets"])
        return {"id": coverage.id, **serialize_coverage(coverage)}


@router.get("/source-coverages")
def api_list_source_coverages(
    request: Request,
    source_id: str | None = Query(default=None),
    industry_id: str | None = Query(default=None),
    region_code: str | None = Query(default=None),
    dimension: str | None = Query(default=None),
    target: str | None = Query(default=None),
    include_unknown: bool = Query(default=False),
    include_disabled: bool = Query(default=False),
    limit: int = Query(default=500, ge=1, le=500),
) -> list[dict]:
    selectors = _selectors(industry_id, region_code, dimension, target)
    stmt = (
        select(SourceCoverage)
        .options(selectinload(SourceCoverage.targets))
        .order_by(SourceCoverage.priority.desc(), SourceCoverage.name, SourceCoverage.id)
    )
    if source_id:
        stmt = stmt.where(SourceCoverage.source_definition_id == source_id)
    if not include_disabled:
        stmt = stmt.where(SourceCoverage.lifecycle_status == "active")
    with session_scope(request.app.state.engine) as session:
        rows = [coverage_to_dict(coverage) for coverage in session.scalars(stmt)]
        if not include_disabled:
            rows = [row for row in rows if coverage_matches(row, [])]
        if selectors:
            rows = [
                row
                for row in rows
                if coverage_matches(row, selectors, include_unknown=include_unknown)
            ]
        return rows[:limit]


@router.get("/source-coverages/summary")
def api_source_coverage_summary(request: Request) -> dict:
    """Operational industry-by-region inventory with unknown kept explicit."""
    stmt = (
        select(SourceCoverage)
        .where(SourceCoverage.lifecycle_status == "active")
        .options(selectinload(SourceCoverage.targets))
    )
    with session_scope(request.app.state.engine) as session:
        coverages = [coverage for coverage in session.scalars(stmt) if match_coverages([coverage])]
        cells: dict[tuple[str, str], dict[str, object]] = {}
        for coverage in coverages:
            industries = sorted(
                {
                    target.target_key
                    for target in coverage.targets
                    if target.dimension == "industry" and target.polarity == "include"
                }
            ) or ["UNKNOWN"]
            regions = sorted(
                {
                    target.target_key
                    for target in coverage.targets
                    if target.dimension == "region" and target.polarity == "include"
                }
            ) or ["UNKNOWN"]
            for industry in industries:
                for region in regions:
                    cell = cells.setdefault(
                        (industry, region),
                        {
                            "industry_id": industry,
                            "region_code": region,
                            "coverage_ids": set(),
                            "source_ids": set(),
                        },
                    )
                    cell["coverage_ids"].add(coverage.id)
                    cell["source_ids"].add(coverage.source_definition_id)
        matrix = []
        for cell in cells.values():
            coverage_ids = sorted(cell.pop("coverage_ids"))
            source_ids = sorted(cell.pop("source_ids"))
            matrix.append(
                {
                    **cell,
                    "coverage_count": len(coverage_ids),
                    "source_count": len(source_ids),
                    "coverage_ids": coverage_ids,
                    "source_ids": source_ids,
                }
            )
        matrix.sort(key=lambda cell: (cell["industry_id"], cell["region_code"]))
        paired_source_ids = {coverage.source_definition_id for coverage in coverages}
        all_source_ids = set(session.scalars(select(SourceDefinition.id)))
        return {
            "matrix": matrix,
            "active_coverage_count": len(coverages),
            "paired_source_count": len(paired_source_ids),
            "unpaired_source_ids": sorted(all_source_ids - paired_source_ids),
        }


@router.post("/source-coverages/{coverage_id}/check-policy")
def api_check_source_coverage_policy(request: Request, coverage_id: str) -> dict:
    with session_scope(request.app.state.engine) as session:
        coverage = session.get(SourceCoverage, coverage_id)
        if coverage is None:
            raise HTTPException(status_code=404, detail="source coverage not found")
        source = session.get(SourceDefinition, coverage.source_definition_id)
        robots_status = check_coverage_robots(request.app.state.settings, source, coverage)
        audit.record(
            session,
            "api",
            "source.coverage_policy_checked",
            "source_coverage",
            coverage.id,
            {"robots_status": robots_status},
        )
        return {
            "id": coverage.id,
            "robots_status": robots_status,
            "robots_checked_at": coverage.robots_checked_at,
        }


@router.patch("/source-coverages/{coverage_id}")
def api_patch_source_coverage(
    request: Request, coverage_id: str, payload: CoveragePatchRequest
) -> dict:
    stmt = (
        select(SourceCoverage)
        .where(SourceCoverage.id == coverage_id)
        .options(selectinload(SourceCoverage.targets))
    )
    with session_scope(request.app.state.engine) as session:
        coverage = session.scalars(stmt).first()
        if coverage is None:
            raise HTTPException(status_code=404, detail="source coverage not found")
        source = session.get(SourceDefinition, coverage.source_definition_id)
        replace_targets = bool(
            {"industry_ids", "region_codes", "targets"} & payload.model_fields_set
        )
        if replace_targets:
            target_specs = tuple(
                TargetSpec(**target) for target in _requests_to_target_values(payload)
            )
        else:
            target_specs = tuple(
                TargetSpec(
                    target.dimension,
                    target.target_key,
                    target.target_label,
                    target.polarity,
                    target.match_mode,
                )
                for target in coverage.targets
            )

        def value(field_name: str):
            return (
                getattr(payload, field_name)
                if field_name in payload.model_fields_set
                else getattr(coverage, field_name)
            )

        try:
            updated, _ = upsert_coverage(
                session,
                source,
                CoverageSpec(
                    coverage_key=coverage.coverage_key,
                    name=value("name"),
                    description=value("description"),
                    lifecycle_status=value("lifecycle_status"),
                    priority=value("priority"),
                    relevance=value("relevance"),
                    confidence=value("confidence"),
                    authority_tier_override=value("authority_tier_override"),
                    collection_scope_override=value("collection_scope_override"),
                    parser_profile_override=value("parser_profile_override"),
                    origin=coverage.origin,
                    origin_pack_id=coverage.origin_pack_id,
                    origin_pack_version=coverage.origin_pack_version,
                    origin_pack_hash=coverage.origin_pack_hash,
                    valid_from=value("valid_from"),
                    valid_to=value("valid_to"),
                    targets=target_specs,
                ),
                actor="api",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.refresh(updated, ["targets"])
        return {"id": updated.id, **serialize_coverage(updated)}


@router.delete("/source-coverages/{coverage_id}")
def api_disable_source_coverage(request: Request, coverage_id: str) -> dict:
    with session_scope(request.app.state.engine) as session:
        coverage = session.get(SourceCoverage, coverage_id)
        if coverage is None:
            raise HTTPException(status_code=404, detail="source coverage not found")
        outcome = disable_coverage(session, coverage, actor="api", reason="disabled through API")
        return {
            "id": coverage.id,
            "lifecycle_status": coverage.lifecycle_status,
            "outcome": outcome,
        }


@router.post("/sources/{source_id}/relationships", status_code=201)
def api_create_source_relationship(
    request: Request, source_id: str, payload: SourceRelationshipRequest
) -> dict:
    if source_id == payload.related_source_definition_id:
        raise HTTPException(status_code=400, detail="a source cannot relate to itself")
    with session_scope(request.app.state.engine) as session:
        if session.get(SourceDefinition, source_id) is None:
            raise HTTPException(status_code=404, detail="source not found")
        if session.get(SourceDefinition, payload.related_source_definition_id) is None:
            raise HTTPException(status_code=404, detail="related source not found")
        exists = session.scalars(
            select(SourceRelationship).where(
                SourceRelationship.source_definition_id == source_id,
                SourceRelationship.related_source_definition_id
                == payload.related_source_definition_id,
                SourceRelationship.relationship_type == payload.relationship_type,
            )
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="source relationship already exists")
        relationship = SourceRelationship(
            source_definition_id=source_id,
            related_source_definition_id=payload.related_source_definition_id,
            relationship_type=payload.relationship_type,
            confidence=payload.confidence,
            origin="user",
            notes=payload.notes,
        )
        session.add(relationship)
        session.flush()
        audit.record(
            session,
            "api",
            "source.relationship_created",
            "source_relationship",
            relationship.id,
            {
                "source_definition_id": source_id,
                "related_source_definition_id": payload.related_source_definition_id,
                "relationship_type": payload.relationship_type,
            },
        )
        return {
            "id": relationship.id,
            "source_definition_id": source_id,
            "related_source_definition_id": payload.related_source_definition_id,
            "relationship_type": relationship.relationship_type,
            "confidence": relationship.confidence,
        }


def _scope_to_dict(scope: ResearchScope) -> dict:
    return {
        "id": scope.id,
        "name": scope.name,
        "description": scope.description,
        "geo_codes": scope.geo_codes,
        "exclude_codes": scope.exclude_codes,
        "industry_ids": scope.industry_ids,
        "target_filters": scope.target_filters,
        "include_unknown": scope.include_unknown,
        "is_active": scope.is_active,
        "created_at": scope.created_at.isoformat(),
        "updated_at": scope.updated_at.isoformat(),
    }


@router.get("/scopes")
def api_list_scopes(request: Request) -> list[dict]:
    with session_scope(request.app.state.engine) as session:
        return [
            _scope_to_dict(scope)
            for scope in session.scalars(select(ResearchScope).order_by(ResearchScope.name))
        ]


@router.get("/scopes/active")
def api_active_scope(request: Request) -> dict:
    with session_scope(request.app.state.engine) as session:
        scope = session.scalars(select(ResearchScope).where(ResearchScope.is_active)).first()
        if scope is None:
            raise HTTPException(status_code=404, detail="no active scope")
        return _scope_to_dict(scope)


@router.post("/scopes", status_code=201)
def api_create_scope(request: Request, payload: ScopeCreateRequest) -> dict:
    with session_scope(request.app.state.engine) as session:
        exists = session.scalars(
            select(ResearchScope).where(ResearchScope.name == payload.name.strip())
        ).first()
        if exists:
            raise HTTPException(status_code=409, detail="scope name already exists")
        try:
            scope = create_scope(
                session,
                payload.name,
                ",".join(payload.geo_codes),
                payload.description,
                actor="api",
                industry_ids_raw=",".join(payload.industry_ids),
                target_filters=payload.target_filters,
                include_unknown=payload.include_unknown,
                exclude_raw=",".join(payload.exclude_codes),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scope_to_dict(scope)


@router.post("/scopes/{scope_id}/activate")
def api_activate_scope(request: Request, scope_id: str) -> dict:
    with session_scope(request.app.state.engine) as session:
        scope = set_active(session, scope_id, actor="api")
        if scope is None:
            raise HTTPException(status_code=404, detail="scope not found")
        return _scope_to_dict(scope)


def _region_to_dict(region) -> dict:
    return {
        "code": region.code,
        "name": region.name,
        "member_codes": region.member_codes,
        "is_builtin": region.is_builtin,
        "updated_at": region.updated_at.isoformat(),
    }


@router.get("/regions")
def api_list_regions(request: Request) -> list[dict]:
    from heatseeker_source_registry.models import GeoRegion
    from heatseeker_source_registry.regions import load_regions

    with session_scope(request.app.state.engine) as session:
        load_regions(session)  # seeds builtins on first call
        return [
            _region_to_dict(region)
            for region in session.scalars(select(GeoRegion).order_by(GeoRegion.code))
        ]


@router.put("/regions", status_code=200)
def api_upsert_region(request: Request, payload: RegionUpsertRequest) -> dict:
    from heatseeker_source_registry.regions import upsert_region

    with session_scope(request.app.state.engine) as session:
        try:
            region = upsert_region(
                session, payload.code, payload.name, payload.member_codes, actor="api"
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _region_to_dict(region)


@router.delete("/regions/{code}", status_code=204)
def api_delete_region(request: Request, code: str) -> None:
    from heatseeker_source_registry.regions import delete_region

    with session_scope(request.app.state.engine) as session:
        try:
            delete_region(session, code, actor="api")
        except ValueError as exc:
            status = 404 if "no region named" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/documents")
def api_list_documents(
    request: Request,
    source_id: str | None = Query(default=None),
    coverage_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    from heatseeker_source_registry.models import SourceDocument

    stmt = select(SourceDocument).order_by(SourceDocument.retrieved_at.desc()).limit(limit)
    if source_id:
        stmt = stmt.where(SourceDocument.source_definition_id == source_id)
    if coverage_id:
        stmt = stmt.where(SourceDocument.source_coverage_id == coverage_id)
    with session_scope(request.app.state.engine) as session:
        return [
            {
                "id": d.id,
                "source_definition_id": d.source_definition_id,
                "source_coverage_id": d.source_coverage_id,
                "source_url": d.source_url,
                "retrieved_at": d.retrieved_at,
                "content_hash": d.content_hash,
                "content_type": d.content_type,
                "size_bytes": d.size_bytes,
                "retrieval_count": d.retrieval_count,
                "http_status": d.http_status,
                "targeting_snapshot": d.targeting_snapshot,
            }
            for d in session.scalars(stmt)
        ]


@router.get("/documents/{document_id}/text")
def api_document_text(
    request: Request, document_id: str, max_chars: int = Query(default=20000, ge=100, le=200000)
) -> dict:
    """Token-lean distilled text for a document — the preferred pipe for AI/agents.

    Distils on demand for documents collected before distillation existed.
    """
    from heatseeker_source_registry import rawstore
    from heatseeker_source_registry.distill import distill_document, read_distilled
    from heatseeker_source_registry.models import SourceDocument

    settings = request.app.state.settings
    with session_scope(request.app.state.engine) as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        text = read_distilled(settings, document)
        if text is None:
            try:
                raw = rawstore.read_bytes(settings, document.raw_storage_path)
            except FileNotFoundError:
                raise HTTPException(status_code=410, detail="raw content missing") from None
            if not distill_document(settings, document, raw):
                raise HTTPException(status_code=415, detail="not distillable (binary content)")
            text = read_distilled(settings, document)
        truncated = len(text) > max_chars
        return {
            "document_id": document.id,
            "source_url": document.source_url,
            "retrieved_at": document.retrieved_at,
            "chars_total": len(text),
            "truncated": truncated,
            "text": text[:max_chars],
        }


@router.get("/packs")
def api_list_packs(request: Request) -> list[dict]:
    rows = []
    with session_scope(request.app.state.engine) as session:
        for pack_path in discover_packs():
            try:
                pack = load_pack(pack_path)
                registered = session.get(PackRegistration, pack.pack_id)
                rows.append(
                    {
                        "pack_id": pack.pack_id,
                        "name": pack.manifest.name,
                        "version": pack.version,
                        "content_hash": pack.content_hash,
                        "valid": True,
                        "registered_version": registered.version if registered else None,
                    }
                )
            except PackValidationError as exc:
                rows.append({"pack_id": pack_path.name, "valid": False, "problems": exc.problems})
    return rows
