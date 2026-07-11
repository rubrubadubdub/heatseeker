"""Adaptive collection cadence + due-collection batches with politeness.

Anti-blocking here means being a *good* robot, never a disguised one (spec §11.4):
identified UA, conditional GETs, Retry-After compliance, inter-request delays with
jitter, and fetching less often when a source rarely changes. Sources that still block
us get degraded toward manual workflows — never evaded.
"""

import random
import time
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry.collect import collect_source, collection_url
from heatseeker_source_registry.models import SourceDefinition, SourceLifecycle

MIN_INTERVAL_SECONDS = 3600.0  # never poll faster than hourly
MAX_INTERVAL_SECONDS = 7 * 86400.0  # never rarer than weekly
SPEED_UP = 0.6  # content changed -> come back sooner
SLOW_DOWN = 1.6  # nothing new -> back off

# Seed cadence from the human-entered expectation, when present.
_FREQUENCY_HINTS = {
    "hourly": 3600.0,
    "daily": 86400.0,
    "weekly": 7 * 86400.0,
    "monthly": 30 * 86400.0,
    "quarterly": 90 * 86400.0,
}


def initial_interval(source: SourceDefinition) -> float:
    hint = (source.expected_update_frequency or "").strip().lower()
    return _FREQUENCY_HINTS.get(hint, 86400.0)


def update_cadence(source: SourceDefinition, outcome: str) -> None:
    """Adapt polling interval to observed change rate; schedule the next collection."""
    interval = source.collect_interval_seconds or initial_interval(source)
    if outcome == "collected":
        interval = max(MIN_INTERVAL_SECONDS, interval * SPEED_UP)
    elif outcome in ("unchanged", "duplicate"):
        interval = min(MAX_INTERVAL_SECONDS, interval * SLOW_DOWN)
    # failures/throttles keep the interval; retry_after_until / degradation gate them
    source.collect_interval_seconds = interval
    jitter = random.uniform(0.0, 0.1 * interval)
    source.next_collect_at = utc_now() + timedelta(seconds=interval + jitter)


def due_sources(session: Session, limit: int) -> list[SourceDefinition]:
    now = utc_now()
    rows = session.scalars(
        select(SourceDefinition)
        .where(
            SourceDefinition.lifecycle_status.in_(
                [SourceLifecycle.ACTIVE, SourceLifecycle.DEGRADED]
            ),
            SourceDefinition.access_method != "manual",
        )
        .order_by(SourceDefinition.authority_tier, SourceDefinition.name)
    ).all()
    due = [
        source
        for source in rows
        if (source.next_collect_at is None or source.next_collect_at <= now)
        and (source.retry_after_until is None or source.retry_after_until <= now)
    ]
    return due[:limit]


def collect_due(
    session: Session,
    settings: Settings,
    limit: int | None = None,
    transport: httpx.BaseTransport | None = None,
    sleeper=time.sleep,
) -> dict:
    """Collect every due source with inter-request politeness delays (jittered; a touch
    longer when consecutive requests hit the same host)."""
    batch = due_sources(session, limit or settings.collect_due_batch_limit)
    outcomes: dict[str, str] = {}
    previous_host: str | None = None
    for index, source in enumerate(batch):
        url = collection_url(source)
        host = urlsplit(url).netloc if url else None
        if index > 0:
            delay = settings.politeness_delay_seconds + random.uniform(
                0.0, settings.politeness_jitter_seconds
            )
            if host and host == previous_host:
                delay *= 2
            sleeper(delay)
        previous_host = host

        result = collect_source(session, settings, source.id, transport=transport)
        outcomes[source.name] = result["outcome"]
        update_cadence(source, result["outcome"])
        session.flush()

    summary: dict[str, int] = {}
    for outcome in outcomes.values():
        summary[outcome] = summary.get(outcome, 0) + 1
    return {"due": len(batch), "outcomes": outcomes, "summary": summary}
