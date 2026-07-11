"""Research scopes: tunable geographic targeting (owner requirement, 2026-07-11).

One active scope at a time. M2 uses it to badge/filter sources; M5+ discovery, lead,
and market workflows must consume the same scope.
"""

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    InvalidGeographyCode,
    excluded_by,
    match_geography,
    normalise_codes,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from heatseeker_source_registry.models import ResearchScope, SourceCoverage, SourceDefinition
from heatseeker_source_registry.targeting import (
    INDUSTRY_DIMENSION,
    REGION_DIMENSION,
    match_coverages,
    normalise_query_filters,
    normalise_target_key,
)

DEFAULT_SCOPES = [
    ("ANZ", "Australia & New Zealand — initial commercial focus", ["ANZ"], True),
    ("APAC", "Asia-Pacific", ["APAC"], False),
    ("Global", "No geographic restriction", ["GLOBAL"], False),
]


def ensure_default_scopes(session: Session) -> None:
    if session.scalars(select(ResearchScope).limit(1)).first() is not None:
        return
    for name, description, codes, active in DEFAULT_SCOPES:
        session.add(
            ResearchScope(name=name, description=description, geo_codes=codes, is_active=active)
        )
    audit.record(session, "system", "scope.defaults_created")


def active_scope(session: Session) -> ResearchScope | None:
    return session.scalars(select(ResearchScope).where(ResearchScope.is_active)).first()


def set_active(session: Session, scope_id: str, actor: str = "user") -> ResearchScope | None:
    scope = session.get(ResearchScope, scope_id)
    if scope is None:
        return None
    session.execute(update(ResearchScope).values(is_active=False))
    scope.is_active = True
    scope.updated_at = utc_now()
    audit.record(
        session,
        actor,
        "scope.activated",
        "research_scope",
        scope.id,
        {
            "name": scope.name,
            "geo_codes": scope.geo_codes,
            "exclude_codes": scope.exclude_codes,
            "industry_ids": scope.industry_ids,
            "target_filters": scope.target_filters,
            "include_unknown": scope.include_unknown,
        },
    )
    return scope


def create_scope(
    session: Session,
    name: str,
    codes_raw: str,
    description: str | None = None,
    actor: str = "user",
    industry_ids_raw: str = "",
    target_filters: dict | None = None,
    include_unknown: bool = True,
    exclude_raw: str = "",
) -> ResearchScope:
    codes = normalise_codes(
        [c for c in codes_raw.replace(";", ",").split(",") if c.strip()],
        validate=True,
    )
    exclude_codes = normalise_codes(
        [c for c in exclude_raw.replace(";", ",").split(",") if c.strip()],
        validate=True,
    )
    if "GLOBAL" in exclude_codes:
        raise InvalidGeographyCode("excluding GLOBAL would exclude everything")
    industries = sorted(
        {
            normalise_target_key(INDUSTRY_DIMENSION, value)
            for value in industry_ids_raw.replace(";", ",").split(",")
            if value.strip()
        }
    )
    normalised_filters = normalise_query_filters(target_filters=target_filters or {})
    scope = ResearchScope(
        name=name.strip(),
        description=description,
        geo_codes=codes,
        exclude_codes=exclude_codes,
        industry_ids=industries,
        target_filters=normalised_filters,
        include_unknown=include_unknown,
    )
    session.add(scope)
    session.flush()
    audit.record(
        session,
        actor,
        "scope.created",
        "research_scope",
        scope.id,
        {
            "name": scope.name,
            "geo_codes": codes,
            "exclude_codes": exclude_codes,
            "industry_ids": industries,
            "target_filters": normalised_filters,
            "include_unknown": include_unknown,
        },
    )
    return scope


def _coverage_excluded(coverage: SourceCoverage, exclude_codes: list) -> bool:
    """A coverage is carved out when its declared region footprint falls entirely
    inside the scope's exclusions; coverages with no region targets are unknown."""
    region_keys = [
        target.target_key
        for target in coverage.targets
        if target.dimension == REGION_DIMENSION and target.polarity == "include"
    ]
    return excluded_by(region_keys, exclude_codes)


def source_in_scope(source: SourceDefinition, scope: ResearchScope | None) -> bool:
    if scope is None:
        return True
    exclude_codes = list(scope.exclude_codes or [])
    if source.coverages:
        coverages = source.coverages
        if exclude_codes:
            coverages = [c for c in coverages if not _coverage_excluded(c, exclude_codes)]
            if not coverages:
                return False  # every declared footprint sits inside the exclusions
        return bool(
            match_coverages(
                coverages,
                industry_ids=scope.industry_ids or (),
                region_codes=scope.geo_codes or (),
                target_filters=scope.target_filters or {},
                include_unknown=scope.include_unknown,
            )
        )
    if exclude_codes and excluded_by(source.geo_codes or [], exclude_codes):
        return False
    return match_geography(
        source.geo_codes or [],
        scope.geo_codes,
        include_unknown=scope.include_unknown,
    )
