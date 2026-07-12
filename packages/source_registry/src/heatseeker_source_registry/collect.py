"""Collection: fetch a source's endpoint, preserve raw evidence, track health (spec §35 M2).

Failure isolation: every outcome is recorded on the source row; one failing source never
affects another (each collection is its own job). Repeated failures degrade the source
instead of hammering it (spec §30.4).
"""

from datetime import timedelta

import httpx
from heatseeker_common import audit
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry import COLLECTOR_VERSION
from heatseeker_source_registry.distill import distill_document
from heatseeker_source_registry.document_pipeline import enqueue_document_processing
from heatseeker_source_registry.document_processing import detect_media_type
from heatseeker_source_registry.fetch import (
    FetchRedirectBlockedError,
    FetchTooLargeError,
    fetch_url,
    response_filename,
)
from heatseeker_source_registry.models import (
    SourceCoverage,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
)
from heatseeker_source_registry.policy import (
    activation_blockers,
    policy_snapshot,
    robots_enforced,
)
from heatseeker_source_registry.publication import extract_claimed_published_at
from heatseeker_source_registry.rawstore import store_bytes
from heatseeker_source_registry.targeting import match_coverage, serialize_coverage

DEGRADE_AFTER_FAILURES = 3


def collection_url(source: SourceDefinition, coverage: SourceCoverage | None = None) -> str | None:
    scope = {
        **(source.collection_scope or {}),
        **((coverage.collection_scope_override or {}) if coverage else {}),
    }
    return scope.get("endpoint_url") or source.base_url


def _targeting_snapshot(
    coverage: SourceCoverage | None, scope_snapshot: dict | None = None
) -> dict:
    if coverage is None:
        return {
            "schema_version": 1,
            "mode": "unscoped",
            "coverage_ids": [],
            "coverages": [],
            "research_scopes": [scope_snapshot] if scope_snapshot else [],
        }
    serialized = {"id": coverage.id, **serialize_coverage(coverage)}
    return {
        "schema_version": 1,
        "mode": "explicit_coverage",
        "coverage_ids": [coverage.id],
        "coverages": [serialized],
        "research_scopes": [scope_snapshot] if scope_snapshot else [],
    }


def _merge_targeting_snapshot(existing: dict | None, incoming: dict) -> dict:
    """Retain every context when one immutable document is seen through many coverages."""
    result = dict(existing or incoming)
    result["schema_version"] = 1
    existing_coverages = {
        row.get("id"): row
        for row in result.get("coverages", [])
        if isinstance(row, dict) and row.get("id")
    }
    for row in incoming.get("coverages", []):
        if isinstance(row, dict) and row.get("id"):
            existing_coverages[row["id"]] = row
    result["coverages"] = [existing_coverages[key] for key in sorted(existing_coverages)]
    result["coverage_ids"] = sorted(existing_coverages)
    scopes = {
        str(row.get("id") or row): row
        for row in result.get("research_scopes", [])
        if isinstance(row, dict)
    }
    for row in incoming.get("research_scopes", []):
        if isinstance(row, dict):
            scopes[str(row.get("id") or row)] = row
    result["research_scopes"] = [scopes[key] for key in sorted(scopes)]
    result["mode"] = "explicit_coverage" if existing_coverages else "unscoped"
    return result


def _record_success(source: SourceDefinition, new_document: bool = False) -> None:
    source.last_success_at = utc_now()
    source.consecutive_failures = 0
    source.last_error = None
    source.fetch_successes += 1
    if new_document:
        source.docs_new += 1
    else:
        source.docs_unchanged += 1
    if source.lifecycle_status == SourceLifecycle.DEGRADED:
        source.lifecycle_status = SourceLifecycle.ACTIVE
    source.updated_at = utc_now()


def _parse_retry_after(value: str | None) -> float:
    """Retry-After seconds (integer form; HTTP-date falls back to a safe default)."""
    if value:
        try:
            return max(float(value), 30.0)
        except ValueError:
            pass  # HTTP-date form — use the conservative default below
    return 900.0


def _record_throttle(session: Session, source: SourceDefinition, retry_after: str | None) -> float:
    """A 429/503 is a politeness instruction, not a source defect: honour it and do not
    count it toward degradation."""
    seconds = _parse_retry_after(retry_after)
    source.retry_after_until = utc_now() + timedelta(seconds=seconds)
    source.last_error = f"throttled — retrying after {int(seconds)}s"
    source.updated_at = utc_now()
    audit.record(
        session,
        "collector",
        "source.throttled",
        "source",
        source.id,
        {"name": source.name, "retry_after_seconds": seconds},
    )
    return seconds


def _record_failure(session: Session, source: SourceDefinition, error: str) -> None:
    source.last_failure_at = utc_now()
    source.consecutive_failures += 1
    source.last_error = error[:2000]
    if (
        source.consecutive_failures >= DEGRADE_AFTER_FAILURES
        and source.lifecycle_status == SourceLifecycle.ACTIVE
    ):
        source.lifecycle_status = SourceLifecycle.DEGRADED
        audit.record(
            session,
            "collector",
            "source.degraded",
            "source",
            source.id,
            {"consecutive_failures": source.consecutive_failures},
        )
    source.updated_at = utc_now()


def collect_source(
    session: Session,
    settings: Settings,
    source_id: str,
    transport: httpx.BaseTransport | None = None,
    coverage_id: str | None = None,
    scope_snapshot: dict | None = None,
    release_before_fetch: bool = False,
) -> dict:
    """Fetch one source endpoint. Returns an outcome dict (also the job result).

    Worker paths set ``release_before_fetch`` so no SQLite read/write transaction spans
    network I/O. Tests and callers with intentionally uncommitted fixtures can retain the
    legacy single-transaction behavior.
    """
    source = session.get(SourceDefinition, source_id)
    if source is None:
        return {"outcome": "error", "error": "source not found"}

    coverage = session.get(SourceCoverage, coverage_id) if coverage_id else None
    if coverage_id and coverage is None:
        return {"outcome": "error", "error": "source coverage not found"}
    if coverage is not None:
        if coverage.source_definition_id != source.id:
            return {"outcome": "error", "error": "coverage belongs to a different source"}
        validity = match_coverage(coverage, {})
        if not validity.matched:
            return {"outcome": "skipped", "error": validity.reason}

    if source.lifecycle_status not in (SourceLifecycle.ACTIVE, SourceLifecycle.DEGRADED):
        return {
            "outcome": "skipped",
            "error": f"source is {source.lifecycle_status}, not collectable",
        }
    enforce_robots = robots_enforced(source, settings)
    blockers = activation_blockers(source, coverage, enforce_robots=enforce_robots)
    if blockers:  # policy may have changed since activation — re-gate every collection
        return {"outcome": "blocked", "error": "; ".join(blockers)}
    url = collection_url(source, coverage)
    if source.access_method == "manual" or not url:
        return {"outcome": "skipped", "error": "manual-only source — add evidence by hand"}

    # Conditional GET against the last retrieval of this URL.
    latest = session.scalars(
        select(SourceDocument)
        .where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.source_url == url,
        )
        .order_by(SourceDocument.retrieved_at.desc())
        .limit(1)
    ).first()
    latest_id = latest.id if latest else None
    latest_etag = latest.etag if latest else None
    latest_last_modified = latest.last_modified if latest else None
    if release_before_fetch:
        session.rollback()

    try:
        result = fetch_url(
            settings,
            url,
            etag=latest_etag,
            last_modified=latest_last_modified,
            transport=transport,
        )
    except (httpx.HTTPError, FetchTooLargeError, FetchRedirectBlockedError) as exc:
        if release_before_fetch:
            source = session.get(SourceDefinition, source_id)
            if source is None:
                return {"outcome": "error", "error": "source removed during collection"}
        source.fetch_attempts += 1
        _record_failure(session, source, f"{type(exc).__name__}: {exc}")
        return {"outcome": "failure", "error": str(exc)}

    if release_before_fetch:
        source = session.get(SourceDefinition, source_id)
        if source is None:
            return {"outcome": "error", "error": "source removed during collection"}
        coverage = session.get(SourceCoverage, coverage_id) if coverage_id else None
        if coverage_id and coverage is None:
            return {"outcome": "error", "error": "coverage removed during collection"}
        latest = session.get(SourceDocument, latest_id) if latest_id else None
        blockers = activation_blockers(source, coverage, enforce_robots=enforce_robots)
        if blockers:
            return {"outcome": "blocked", "error": "; ".join(blockers)}
    source.fetch_attempts += 1

    if result.not_modified and latest is not None:
        latest.last_seen_at = utc_now()
        latest.retrieval_count += 1
        snapshot = _targeting_snapshot(coverage, scope_snapshot)
        latest.targeting_snapshot = _merge_targeting_snapshot(latest.targeting_snapshot, snapshot)
        if latest.source_coverage_id is None and coverage is not None:
            latest.source_coverage_id = coverage.id
        _record_success(source)
        enqueue_document_processing(session, settings, latest)
        return {
            "outcome": "unchanged",
            "document_id": latest.id,
            "http_status": 304,
            "source_id": source.id,
            "source_coverage_id": coverage.id if coverage else None,
            "coverage_ids": latest.targeting_snapshot["coverage_ids"],
        }
    if result.not_modified:
        _record_failure(session, source, "HTTP 304 received but prior evidence disappeared")
        return {"outcome": "failure", "error": "304 without prior evidence"}

    if result.status_code in (429, 503):
        seconds = _record_throttle(session, source, result.retry_after)
        return {"outcome": "throttled", "retry_after_seconds": seconds, "source_id": source.id}

    if result.status_code >= 400:
        _record_failure(session, source, f"HTTP {result.status_code} from {url}")
        return {"outcome": "failure", "error": f"HTTP {result.status_code}"}

    rel_path, digest = store_bytes(settings, result.content, result.content_type)

    duplicate = session.scalars(
        select(SourceDocument).where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.source_url == url,
            SourceDocument.content_hash == digest,
        )
    ).first()
    if duplicate is not None:
        duplicate.last_seen_at = utc_now()
        duplicate.retrieval_count += 1
        duplicate.etag = result.etag or duplicate.etag
        duplicate.last_modified = result.last_modified or duplicate.last_modified
        duplicate.targeting_snapshot = _merge_targeting_snapshot(
            duplicate.targeting_snapshot, _targeting_snapshot(coverage, scope_snapshot)
        )
        if duplicate.source_coverage_id is None and coverage is not None:
            duplicate.source_coverage_id = coverage.id
        _record_success(source)
        enqueue_document_processing(session, settings, duplicate)
        return {
            "outcome": "duplicate",
            "document_id": duplicate.id,
            "content_hash": digest,
            "source_id": source.id,
            "source_coverage_id": coverage.id if coverage else None,
            "coverage_ids": duplicate.targeting_snapshot["coverage_ids"],
        }

    document = SourceDocument(
        source_definition_id=source.id,
        source_coverage_id=coverage.id if coverage else None,
        source_url=url,
        canonical_url=result.final_url if result.final_url != url else None,
        content_hash=digest,
        content_type=result.content_type,
        detected_content_type=detect_media_type(
            result.content,
            result.content_type,
            response_filename(result.content_disposition, result.final_url),
        ),
        content_disposition=result.content_disposition,
        original_filename=response_filename(result.content_disposition, result.final_url),
        claimed_published_at=(
            extract_claimed_published_at(result.content)
            if "html" in (result.content_type or "").lower()
            else None
        ),
        size_bytes=len(result.content),
        raw_storage_path=rel_path,
        http_status=result.status_code,
        etag=result.etag,
        last_modified=result.last_modified,
        access_policy_snapshot=policy_snapshot(
            source,
            coverage=coverage,
            collection_url=url,
            enforce_robots=enforce_robots,
        ),
        targeting_snapshot=_targeting_snapshot(coverage, scope_snapshot),
        collector_version=COLLECTOR_VERSION,
    )
    session.add(document)
    session.flush()
    distill_document(settings, document, result.content)  # token-lean text pipe
    enqueue_document_processing(session, settings, document)
    _record_success(source, new_document=True)
    audit.record(
        session,
        "collector",
        "document.collected",
        "source_document",
        document.id,
        {
            "source": source.name,
            "bytes": len(result.content),
            "hash": digest[:12],
            "distilled_chars": document.distilled_chars,
        },
    )
    return {
        "outcome": "collected",
        "document_id": document.id,
        "content_hash": digest,
        "bytes": len(result.content),
        "source_id": source.id,
        "source_coverage_id": coverage.id if coverage else None,
        "coverage_ids": [coverage.id] if coverage else [],
    }
