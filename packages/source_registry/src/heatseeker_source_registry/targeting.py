"""Source applicability tuples, deterministic matching, and mutation services.

A coverage is one coherent tuple.  Dimensions inside it are conjunctive, included
values inside a dimension are alternatives, exclusions veto, and multiple coverages
are alternatives.  This preserves pairings such as ``scaffolding + AU`` without
accidentally inferring a Cartesian product with another coverage.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    GeographyMatchMode,
    InvalidGeographyCode,
    match_geography,
    validate_code,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceCoverage,
    SourceCoverageTarget,
    SourceDefinition,
)

Polarity = Literal["include", "exclude"]
MatchMode = Literal["exact", "hierarchical", "covers", "within"]
CoverageMutation = Literal["created", "updated", "unchanged", "disabled"]

INDUSTRY_DIMENSION = "industry"
REGION_DIMENSION = "region"
GLOBAL_TARGET = "GLOBAL"
_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_GENERIC_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/]*$")


class CoverageValidationError(ValueError):
    """A coverage would be ambiguous or violate targeting invariants."""


def validate_collection_scope(value: dict | None) -> dict | None:
    """Validate endpoint configuration without allowing inline credentials/secrets."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CoverageValidationError("collection scope must be an object")
    forbidden = {"api_key", "password", "token", "secret", "authorization"}
    exposed = forbidden & {str(key).strip().lower() for key in value}
    if exposed:
        raise CoverageValidationError(
            "collection scope must reference credentials, not contain secret fields: "
            + ", ".join(sorted(exposed))
        )
    endpoint = value.get("endpoint_url")
    if endpoint is not None:
        if not isinstance(endpoint, str):
            raise CoverageValidationError("collection endpoint_url must be a string")
        parts = urlsplit(endpoint.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise CoverageValidationError("collection endpoint_url must be an absolute HTTP(S) URL")
        if parts.username or parts.password:
            raise CoverageValidationError("collection endpoint_url must not contain credentials")
    result = dict(value)
    allowed_origins = value.get("allowed_origins", [])
    if not isinstance(allowed_origins, list) or len(allowed_origins) > 20:
        raise CoverageValidationError("collection allowed_origins must be a list of at most 20")
    normalised_origins: list[str] = []
    for origin in allowed_origins:
        if not isinstance(origin, str):
            raise CoverageValidationError("each allowed origin must be a string")
        parts = urlsplit(origin.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise CoverageValidationError(
                "allowed origins must be HTTP(S) origins without credentials or paths"
            )
        normalised_origins.append(f"{parts.scheme}://{parts.netloc.lower()}")
    if normalised_origins:
        result["allowed_origins"] = sorted(set(normalised_origins))
    return result


def normalise_dimension(value: str) -> str:
    dimension = value.strip().lower().replace("-", "_").replace(" ", "_")
    dimension = {"geo": REGION_DIMENSION, "geography": REGION_DIMENSION}.get(dimension, dimension)
    if len(dimension) > 100 or not _DIMENSION_RE.fullmatch(dimension):
        raise CoverageValidationError(f"invalid dimension: {value!r}")
    return dimension


def normalise_target_key(dimension: str, value: str) -> str:
    dimension = normalise_dimension(dimension)
    if dimension == REGION_DIMENSION:
        try:
            code = validate_code(value)
            if len(code) > 300:
                raise CoverageValidationError("region target is too long")
            return code
        except InvalidGeographyCode as exc:
            raise CoverageValidationError(str(exc)) from exc
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if len(key) > 300 or not _GENERIC_KEY_RE.fullmatch(key):
        raise CoverageValidationError(f"invalid {dimension} target: {value!r}")
    return key


@dataclass(frozen=True, slots=True)
class TargetSpec:
    dimension: str
    target_key: str
    target_label: str | None = None
    polarity: Polarity = "include"
    match_mode: MatchMode | None = None

    def normalised(self) -> TargetSpec:
        dimension = normalise_dimension(self.dimension)
        polarity = self.polarity
        if polarity not in ("include", "exclude"):
            raise CoverageValidationError(f"invalid target polarity: {polarity!r}")
        mode = self.match_mode or ("hierarchical" if dimension == REGION_DIMENSION else "exact")
        if mode not in ("exact", "hierarchical", "covers", "within"):
            raise CoverageValidationError(f"invalid target match mode: {mode!r}")
        if dimension != REGION_DIMENSION and mode != "exact":
            raise CoverageValidationError(
                f"{mode} matching is only supported for {REGION_DIMENSION!r}"
            )
        label = self.target_label.strip() if self.target_label else None
        if label and len(label) > 500:
            raise CoverageValidationError("target label must not exceed 500 characters")
        return TargetSpec(
            dimension=dimension,
            target_key=normalise_target_key(dimension, self.target_key),
            target_label=label or None,
            polarity=polarity,
            match_mode=mode,
        )

    def as_dict(self) -> dict[str, str | None]:
        target = self.normalised()
        return {
            "dimension": target.dimension,
            "target_key": target.target_key,
            "target_label": target.target_label,
            "polarity": target.polarity,
            "match_mode": target.match_mode,
        }


@dataclass(frozen=True, slots=True)
class CoverageSpec:
    coverage_key: str
    targets: tuple[TargetSpec, ...] = ()
    name: str | None = None
    description: str | None = None
    lifecycle_status: Literal["active", "disabled"] = "active"
    priority: int = 50
    relevance: float = 1.0
    confidence: float = 1.0
    authority_tier_override: int | None = None
    collection_scope_override: dict | None = None
    parser_profile_override: str | None = None
    origin: str = "user"
    origin_pack_id: str | None = None
    origin_pack_version: str | None = None
    origin_pack_hash: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def normalised(self) -> CoverageSpec:
        key = self.coverage_key.strip().lower()
        if len(key) > 200 or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", key):
            raise CoverageValidationError(f"invalid coverage key: {self.coverage_key!r}")
        if self.lifecycle_status not in ("active", "disabled"):
            raise CoverageValidationError(f"invalid coverage lifecycle: {self.lifecycle_status!r}")
        if not 0 <= self.priority <= 100:
            raise CoverageValidationError("coverage priority must be between 0 and 100")
        for label, score in (("relevance", self.relevance), ("confidence", self.confidence)):
            if score is not None and not 0 <= score <= 1:
                raise CoverageValidationError(f"{label} must be between 0 and 1")
        if self.authority_tier_override is not None and not 1 <= self.authority_tier_override <= 7:
            raise CoverageValidationError("authority tier override must be between 1 and 7")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise CoverageValidationError("coverage valid_from must not be after valid_to")
        for label, value in (("valid_from", self.valid_from), ("valid_to", self.valid_to)):
            if value is not None and value.tzinfo is None:
                raise CoverageValidationError(f"coverage {label} must include a timezone")
        if self.origin == "pack_seed" and not all(
            (self.origin_pack_id, self.origin_pack_version, self.origin_pack_hash)
        ):
            raise CoverageValidationError("pack_seed coverage requires complete pack provenance")
        name = self.name.strip() if self.name else key
        if len(name) > 300:
            raise CoverageValidationError("coverage name must not exceed 300 characters")
        description = self.description.strip() if self.description else None
        if description and len(description) > 20_000:
            raise CoverageValidationError("coverage description must not exceed 20000 characters")
        parser_profile = (
            self.parser_profile_override.strip() if self.parser_profile_override else None
        )
        if parser_profile and len(parser_profile) > 200:
            raise CoverageValidationError("parser profile must not exceed 200 characters")
        origin = self.origin.strip().lower()
        if not origin or len(origin) > 50:
            raise CoverageValidationError("coverage origin is invalid")
        for label, value, limit in (
            ("origin_pack_id", self.origin_pack_id, 100),
            ("origin_pack_version", self.origin_pack_version, 100),
            ("origin_pack_hash", self.origin_pack_hash, 64),
        ):
            if value is not None and len(value) > limit:
                raise CoverageValidationError(f"{label} exceeds {limit} characters")
        return CoverageSpec(
            coverage_key=key,
            targets=normalise_targets(self.targets),
            name=name,
            description=description,
            lifecycle_status=self.lifecycle_status,
            priority=self.priority,
            relevance=self.relevance,
            confidence=self.confidence,
            authority_tier_override=self.authority_tier_override,
            collection_scope_override=validate_collection_scope(self.collection_scope_override),
            parser_profile_override=parser_profile,
            origin=origin,
            origin_pack_id=self.origin_pack_id,
            origin_pack_version=self.origin_pack_version,
            origin_pack_hash=self.origin_pack_hash,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
        )


def _target_from_value(value: TargetSpec | SourceCoverageTarget | Mapping[str, Any]) -> TargetSpec:
    if isinstance(value, TargetSpec):
        return value.normalised()
    if isinstance(value, Mapping):
        return TargetSpec(
            dimension=str(value["dimension"]),
            target_key=str(value.get("target_key", value.get("key", value.get("value", "")))),
            target_label=value.get("target_label", value.get("label")),
            polarity=value.get("polarity", "include"),
            match_mode=value.get("match_mode"),
        ).normalised()
    return TargetSpec(
        dimension=value.dimension,
        target_key=value.target_key,
        target_label=value.target_label,
        polarity=value.polarity,
        match_mode=value.match_mode,
    ).normalised()


def normalise_targets(
    values: Iterable[TargetSpec | SourceCoverageTarget | Mapping[str, Any]],
) -> tuple[TargetSpec, ...]:
    """Normalise, sort, de-duplicate, and reject contradictory targets."""
    by_identity: dict[tuple[str, str], TargetSpec] = {}
    for raw in values:
        target = _target_from_value(raw)
        identity = (target.dimension, target.target_key)
        previous = by_identity.get(identity)
        if previous is not None and previous.polarity != target.polarity:
            raise CoverageValidationError(
                f"{target.dimension}:{target.target_key} cannot be both included and excluded"
            )
        if previous is None:
            by_identity[identity] = target
        elif previous.match_mode != target.match_mode:
            raise CoverageValidationError(
                f"{target.dimension}:{target.target_key} has conflicting match modes"
            )
        elif not previous.target_label and target.target_label:
            by_identity[identity] = target
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.dimension,
                item.target_key,
                item.polarity,
                item.match_mode or "",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class DimensionExplanation:
    dimension: str
    outcome: Literal["matched", "unknown", "excluded", "no_match"]
    requested: tuple[str, ...]
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "outcome": self.outcome,
            "requested": list(self.requested),
            "includes": list(self.includes),
            "excludes": list(self.excludes),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CoverageExplanation:
    coverage_key: str
    matched: bool
    dimensions: tuple[DimensionExplanation, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "coverage_key": self.coverage_key,
            "matched": self.matched,
            "reason": self.reason,
            "dimensions": [dimension.as_dict() for dimension in self.dimensions],
        }


@dataclass(frozen=True, slots=True)
class SourceMatchExplanation:
    matched: bool
    include_unknown: bool
    matched_coverage_keys: tuple[str, ...]
    coverages: tuple[CoverageExplanation, ...]

    def __bool__(self) -> bool:
        return self.matched

    def as_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "include_unknown": self.include_unknown,
            "matched_coverage_keys": list(self.matched_coverage_keys),
            "coverages": [coverage.as_dict() for coverage in self.coverages],
        }


def _normalise_filter_values(dimension: str, values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence):
        raise CoverageValidationError(f"filter values for {dimension!r} must be a list")
    return tuple(sorted({normalise_target_key(dimension, str(value)) for value in values}))


def normalise_query_filters(
    *,
    industry_ids: Iterable[str] = (),
    region_codes: Iterable[str] = (),
    target_filters: Mapping[str, object] | None = None,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Return deterministic ``dimension -> include/exclude`` query filters."""
    if isinstance(industry_ids, str):
        industry_ids = (industry_ids,)
    if isinstance(region_codes, str):
        region_codes = (region_codes,)
    filters: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"include": set(), "exclude": set()}
    )
    filters[INDUSTRY_DIMENSION]["include"].update(
        normalise_target_key(INDUSTRY_DIMENSION, value) for value in industry_ids
    )
    filters[REGION_DIMENSION]["include"].update(
        normalise_target_key(REGION_DIMENSION, value) for value in region_codes
    )
    for raw_dimension, raw_filter in (target_filters or {}).items():
        dimension = normalise_dimension(raw_dimension)
        if isinstance(raw_filter, Mapping):
            includes = _normalise_filter_values(dimension, raw_filter.get("include", ()))
            excludes = _normalise_filter_values(dimension, raw_filter.get("exclude", ()))
        else:
            includes = _normalise_filter_values(dimension, raw_filter)
            excludes = ()
        filters[dimension]["include"].update(includes)
        filters[dimension]["exclude"].update(excludes)
    return {
        dimension: {
            "include": tuple(sorted(values["include"])),
            "exclude": tuple(sorted(values["exclude"])),
        }
        for dimension, values in sorted(filters.items())
        if values["include"] or values["exclude"]
    }


def _keys_related(dimension: str, coverage_target: TargetSpec, requested: str) -> bool:
    if dimension == REGION_DIMENSION:
        modes = {
            "hierarchical": GeographyMatchMode.OVERLAPS,
            "covers": GeographyMatchMode.COVERS,
            "within": GeographyMatchMode.WITHIN,
        }
        if coverage_target.match_mode in modes:
            return match_geography(
                [coverage_target.target_key],
                [requested],
                mode=modes[coverage_target.match_mode],
                include_unknown=False,
            )
    return coverage_target.target_key == requested


def _dimension_match(
    dimension: str,
    targets: Sequence[TargetSpec],
    query: Mapping[str, tuple[str, ...]],
    include_unknown: bool,
) -> DimensionExplanation:
    includes = tuple(target for target in targets if target.polarity == "include")
    excludes = tuple(target for target in targets if target.polarity == "exclude")
    requested = query.get("include", ())
    requested_excludes = query.get("exclude", ())
    include_keys = tuple(target.target_key for target in includes)
    exclude_keys = tuple(target.target_key for target in excludes)

    vetoed = sorted(
        request
        for request in requested
        if any(_keys_related(dimension, target, request) for target in excludes)
    )
    if vetoed:
        return DimensionExplanation(
            dimension,
            "excluded",
            requested,
            include_keys,
            exclude_keys,
            f"coverage exclusion vetoed: {', '.join(vetoed)}",
        )
    scope_vetoes = sorted(
        target.target_key
        for target in includes
        if any(_keys_related(dimension, target, value) for value in requested_excludes)
    )
    if scope_vetoes:
        return DimensionExplanation(
            dimension,
            "excluded",
            requested,
            include_keys,
            exclude_keys,
            f"scope exclusion vetoed: {', '.join(scope_vetoes)}",
        )
    if not requested:
        return DimensionExplanation(
            dimension,
            "matched",
            requested,
            include_keys,
            exclude_keys,
            "dimension has no include constraint",
        )
    if not includes:
        return DimensionExplanation(
            dimension,
            "unknown",
            requested,
            include_keys,
            exclude_keys,
            "coverage has no value for this dimension"
            + ("; included by policy" if include_unknown else "; excluded by policy"),
        )
    if dimension == REGION_DIMENSION and any(
        target.target_key == GLOBAL_TARGET for target in includes
    ):
        return DimensionExplanation(
            dimension,
            "matched",
            requested,
            include_keys,
            exclude_keys,
            "explicit GLOBAL coverage",
        )
    if any(
        _keys_related(dimension, target, request) for target in includes for request in requested
    ):
        return DimensionExplanation(
            dimension,
            "matched",
            requested,
            include_keys,
            exclude_keys,
            "at least one included value matched",
        )
    return DimensionExplanation(
        dimension,
        "no_match",
        requested,
        include_keys,
        exclude_keys,
        "no included value matched",
    )


def coverage_targets(coverage: SourceCoverage | CoverageSpec) -> tuple[TargetSpec, ...]:
    return normalise_targets(coverage.targets)


def match_coverage(
    coverage: SourceCoverage | CoverageSpec,
    filters: Mapping[str, Mapping[str, tuple[str, ...]]],
    *,
    include_unknown: bool = False,
    at: datetime | None = None,
) -> CoverageExplanation:
    status = coverage.lifecycle_status
    key = coverage.coverage_key
    if status != "active":
        return CoverageExplanation(key, False, (), f"coverage is {status}")
    at = at or utc_now()
    if coverage.valid_from is not None and coverage.valid_from > at:
        return CoverageExplanation(key, False, (), "coverage is not valid yet")
    if coverage.valid_to is not None and coverage.valid_to < at:
        return CoverageExplanation(key, False, (), "coverage has expired")
    grouped: dict[str, list[TargetSpec]] = defaultdict(list)
    for target in coverage_targets(coverage):
        grouped[target.dimension].append(target)
    explanations = tuple(
        _dimension_match(dimension, grouped.get(dimension, ()), query, include_unknown)
        for dimension, query in sorted(filters.items())
    )
    matched = all(
        explanation.outcome == "matched" or (include_unknown and explanation.outcome == "unknown")
        for explanation in explanations
    )
    return CoverageExplanation(
        key,
        matched,
        explanations,
        "all dimensions matched" if matched else "one or more dimensions did not match",
    )


def match_coverages(
    coverages: Iterable[SourceCoverage | CoverageSpec],
    *,
    industry_ids: Iterable[str] = (),
    region_codes: Iterable[str] = (),
    target_filters: Mapping[str, object] | None = None,
    include_unknown: bool = False,
    at: datetime | None = None,
) -> SourceMatchExplanation:
    filters = normalise_query_filters(
        industry_ids=industry_ids,
        region_codes=region_codes,
        target_filters=target_filters,
    )
    explanations = tuple(
        match_coverage(coverage, filters, include_unknown=include_unknown, at=at)
        for coverage in sorted(coverages, key=lambda item: item.coverage_key)
    )
    matched_keys = tuple(
        explanation.coverage_key for explanation in explanations if explanation.matched
    )
    return SourceMatchExplanation(bool(matched_keys), include_unknown, matched_keys, explanations)


def serialize_coverage(coverage: SourceCoverage | CoverageSpec) -> dict[str, object]:
    """Stable representation suitable for APIs, audit records, and snapshot tests."""
    return {
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
        "robots_status": getattr(coverage, "robots_status", RobotsStatus.UNKNOWN),
        "robots_checked_at": (
            coverage.robots_checked_at.isoformat()
            if getattr(coverage, "robots_checked_at", None)
            else None
        ),
        "origin": coverage.origin,
        "origin_pack_id": coverage.origin_pack_id,
        "origin_pack_version": coverage.origin_pack_version,
        "origin_pack_hash": coverage.origin_pack_hash,
        "valid_from": coverage.valid_from.isoformat() if coverage.valid_from else None,
        "valid_to": coverage.valid_to.isoformat() if coverage.valid_to else None,
        "targets": [target.as_dict() for target in coverage_targets(coverage)],
    }


_COVERAGE_SCALAR_FIELDS = (
    "name",
    "description",
    "lifecycle_status",
    "priority",
    "relevance",
    "confidence",
    "authority_tier_override",
    "collection_scope_override",
    "parser_profile_override",
    "origin",
    "origin_pack_id",
    "origin_pack_version",
    "origin_pack_hash",
    "valid_from",
    "valid_to",
)


def upsert_coverage(
    session: Session,
    source: SourceDefinition,
    spec: CoverageSpec,
    *,
    actor: str = "system",
    audit_changes: bool = True,
) -> tuple[SourceCoverage, CoverageMutation]:
    """Create or exactly reconcile one coverage without committing the transaction."""
    desired = spec.normalised()
    coverage = session.scalars(
        select(SourceCoverage).where(
            SourceCoverage.source_definition_id == source.id,
            SourceCoverage.coverage_key == desired.coverage_key,
        )
    ).first()
    if coverage is None:
        coverage = SourceCoverage(
            source_definition_id=source.id,
            coverage_key=desired.coverage_key,
            **{field_name: getattr(desired, field_name) for field_name in _COVERAGE_SCALAR_FIELDS},
        )
        session.add(coverage)
        session.flush()
        for target in desired.targets:
            session.add(
                SourceCoverageTarget(
                    source_coverage_id=coverage.id,
                    **target.as_dict(),
                )
            )
        session.flush()
        if audit_changes:
            audit.record(
                session,
                actor,
                "source.coverage_created",
                "source_coverage",
                coverage.id,
                serialize_coverage(desired),
            )
        return coverage, "created"

    changed = False
    old_endpoint = (coverage.collection_scope_override or {}).get("endpoint_url")
    for field_name in _COVERAGE_SCALAR_FIELDS:
        value = getattr(desired, field_name)
        if getattr(coverage, field_name) != value:
            setattr(coverage, field_name, value)
            changed = True
    new_endpoint = (coverage.collection_scope_override or {}).get("endpoint_url")
    if old_endpoint != new_endpoint:
        coverage.robots_status = RobotsStatus.UNKNOWN
        coverage.robots_checked_at = None
    existing_targets = coverage_targets(coverage)
    if existing_targets != desired.targets:
        for target in list(coverage.targets):
            session.delete(target)
        session.flush()
        for target in desired.targets:
            session.add(
                SourceCoverageTarget(
                    source_coverage_id=coverage.id,
                    **target.as_dict(),
                )
            )
        changed = True
    if not changed:
        return coverage, "unchanged"
    coverage.updated_at = utc_now()
    session.flush()
    if audit_changes:
        audit.record(
            session,
            actor,
            "source.coverage_updated",
            "source_coverage",
            coverage.id,
            serialize_coverage(desired),
        )
    return coverage, "updated"


def disable_coverage(
    session: Session,
    coverage: SourceCoverage,
    *,
    actor: str = "system",
    reason: str | None = None,
    audit_changes: bool = True,
) -> CoverageMutation:
    """Soft-disable a coverage; source identity and evidence are never deleted."""
    if coverage.lifecycle_status == "disabled":
        return "unchanged"
    coverage.lifecycle_status = "disabled"
    coverage.updated_at = utc_now()
    if audit_changes:
        audit.record(
            session,
            actor,
            "source.coverage_disabled",
            "source_coverage",
            coverage.id,
            {"coverage_key": coverage.coverage_key, "reason": reason},
        )
    return "disabled"
