"""Access-policy checks: robots evaluation and the activation gate (spec §11.3-§11.4).

Robots permission is never treated as legal authorisation (§11.3); terms status is a
separate human judgement. The activation gate is deterministic code — AI may inform the
terms review, never bypass this gate (ADR-0008 boundary).
"""

from urllib.parse import urlsplit

import httpx
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from protego import Protego

from heatseeker_source_registry.identity import canonicalise_url
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceCoverage,
    SourceDefinition,
    TermsStatus,
)


def _source_policy_url(source: SourceDefinition) -> str | None:
    return (source.collection_scope or {}).get("endpoint_url") or source.base_url


def _coverage_policy_url(source: SourceDefinition, coverage: SourceCoverage | None) -> str | None:
    return (
        (coverage.collection_scope_override or {}).get("endpoint_url") if coverage else None
    ) or _source_policy_url(source)


def coverage_has_distinct_endpoint(
    source: SourceDefinition, coverage: SourceCoverage | None
) -> bool:
    coverage_url = _coverage_policy_url(source, coverage)
    source_url = _source_policy_url(source)
    try:
        distinct = bool(
            coverage_url
            and source_url
            and canonicalise_url(coverage_url) != canonicalise_url(source_url)
        )
    except ValueError:
        distinct = coverage_url != source_url
    return bool(
        coverage and (coverage.collection_scope_override or {}).get("endpoint_url") and distinct
    )


def _check_url_robots(
    settings: Settings,
    url: str | None,
    access_method: str,
    transport: httpx.BaseTransport | None,
) -> RobotsStatus:
    if access_method == "manual" or not url:
        return RobotsStatus.NOT_APPLICABLE
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    path = parts.path or "/"
    try:
        with httpx.Client(
            timeout=settings.fetch_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.crawler_user_agent},
            transport=transport,
        ) as client:
            response = client.get(robots_url)
        if response.status_code >= 500:
            return RobotsStatus.UNREACHABLE
        if response.status_code >= 400:
            # RFC 9309: unavailable robots.txt (4xx) means unrestricted.
            return RobotsStatus.ALLOWED
        parsed = Protego.parse(response.text)
        agent = settings.crawler_user_agent.split("/")[0]
        return RobotsStatus.ALLOWED if parsed.can_fetch(path, agent) else RobotsStatus.DISALLOWED
    except httpx.HTTPError:
        return RobotsStatus.UNREACHABLE


def check_robots(
    settings: Settings,
    source: SourceDefinition,
    transport: httpx.BaseTransport | None = None,
) -> RobotsStatus:
    """Fetch robots.txt for the source's effective endpoint and update the source."""
    status = _check_url_robots(
        settings, _source_policy_url(source), source.access_method, transport
    )

    source.robots_status = status
    source.robots_checked_at = utc_now()
    source.updated_at = utc_now()
    return status


def check_coverage_robots(
    settings: Settings,
    source: SourceDefinition,
    coverage: SourceCoverage,
    transport: httpx.BaseTransport | None = None,
) -> RobotsStatus:
    """Check a coverage-specific endpoint without overwriting source-level policy."""
    if not coverage_has_distinct_endpoint(source, coverage):
        return check_robots(settings, source, transport=transport)
    status = _check_url_robots(
        settings,
        _coverage_policy_url(source, coverage),
        source.access_method,
        transport,
    )
    coverage.robots_status = status
    coverage.robots_checked_at = utc_now()
    coverage.updated_at = utc_now()
    return status


def activation_blockers(
    source: SourceDefinition, coverage: SourceCoverage | None = None
) -> list[str]:
    """Deterministic activation gate. Empty list = activatable."""
    blockers: list[str] = []
    if source.terms_status == TermsStatus.PROHIBITED:
        blockers.append("terms prohibit automated access (spec §11.4)")
    if source.access_method != "manual":
        robots_status = (
            coverage.robots_status
            if coverage_has_distinct_endpoint(source, coverage)
            else source.robots_status
        )
        if robots_status == RobotsStatus.DISALLOWED:
            blockers.append("robots.txt disallows collection (spec §11.3)")
        elif robots_status in (RobotsStatus.UNKNOWN, RobotsStatus.UNREACHABLE):
            qualifier = (
                " for this coverage endpoint"
                if coverage_has_distinct_endpoint(source, coverage)
                else ""
            )
            blockers.append(f"robots.txt not yet verified{qualifier} — run a policy check first")
    return blockers


def policy_snapshot(
    source: SourceDefinition,
    *,
    coverage: SourceCoverage | None = None,
    collection_url: str | None = None,
) -> dict:
    """Recorded with every retrieval so evidence carries the policy it was taken under."""
    return {
        "robots_status": (
            coverage.robots_status
            if coverage_has_distinct_endpoint(source, coverage)
            else source.robots_status
        ),
        "robots_checked_at": (
            coverage.robots_checked_at.isoformat()
            if coverage_has_distinct_endpoint(source, coverage)
            and coverage
            and coverage.robots_checked_at
            else source.robots_checked_at.isoformat()
            if source.robots_checked_at
            else None
        ),
        "terms_status": source.terms_status,
        "lifecycle_status": source.lifecycle_status,
        "authority_tier": (
            coverage.authority_tier_override
            if coverage and coverage.authority_tier_override is not None
            else source.authority_tier
        ),
        "source_authority_tier": source.authority_tier,
        "source_coverage_id": coverage.id if coverage else None,
        "collection_url": collection_url or source.base_url,
        "policy_checked_url": _coverage_policy_url(source, coverage),
        "policy_level": (
            "coverage_endpoint"
            if coverage_has_distinct_endpoint(source, coverage)
            else "source_endpoint"
        ),
    }
