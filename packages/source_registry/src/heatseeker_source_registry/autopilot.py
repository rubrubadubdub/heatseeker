"""Autopilot: drives the M2 funnel without human handling (owner directive 2026-07-11).

Each tick: bootstrap seeds from packs -> check unchecked robots policies (bounded batch)
-> auto-activate cleared candidates -> collect due sources -> periodic maintenance
(grading, auto-deprecation, robots re-checks). Deterministic gates still apply: terms-
prohibited sources never activate; robots-disallowed sources activate only when the
auditable global/per-source policy says robots is advisory. Proposal-origin sources still
wait for review (source-discovery.md funnel). Humans can intervene anytime.
"""

from datetime import timedelta

import httpx
from heatseeker_common import audit
from heatseeker_common.models import AppMeta
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_industry_packs.loader import PackValidationError, discover_packs, load_pack
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from heatseeker_source_registry.grading import evaluate_all
from heatseeker_source_registry.models import RobotsStatus, SourceDefinition, SourceLifecycle
from heatseeker_source_registry.policy import activation_blockers, check_robots, robots_enforced
from heatseeker_source_registry.regions import load_regions
from heatseeker_source_registry.schedule import collect_due
from heatseeker_source_registry.sync import sync_pack_seeds

_MAINT_KEY = "autopilot.last_maintenance"
AUTO_ACTIVATE_ORIGINS = ("pack_seed", "user")  # proposals still need review


def _bootstrap_seeds(session: Session) -> int:
    """Sync pack seeds when the registry is empty (first run / new pack)."""
    if session.scalars(select(SourceDefinition.id).limit(1)).first() is not None:
        return 0
    synced = 0
    for pack_path in discover_packs():
        try:
            pack = load_pack(pack_path)
        except PackValidationError:
            continue
        result = sync_pack_seeds(session, pack, actor="autopilot")
        synced += result.get("sources_created", result.get("created", 0))
    return synced


def _check_policies(
    session: Session, settings: Settings, transport: httpx.BaseTransport | None
) -> int:
    recheck_cutoff = utc_now() - timedelta(hours=1)
    pending = session.scalars(
        select(SourceDefinition)
        .where(
            SourceDefinition.access_method != "manual",
            SourceDefinition.robots_status.in_([RobotsStatus.UNKNOWN, RobotsStatus.UNREACHABLE]),
            or_(
                SourceDefinition.robots_checked_at.is_(None),
                SourceDefinition.robots_checked_at < recheck_cutoff,
            ),
        )
        .order_by(SourceDefinition.authority_tier)
        .limit(settings.autopilot_policy_batch)
    ).all()
    for source in pending:
        check_robots(settings, source, transport=transport)
    return len(pending)


def _auto_activate(session: Session, settings: Settings) -> list[str]:
    activated = []
    candidates = session.scalars(
        select(SourceDefinition).where(
            SourceDefinition.lifecycle_status == SourceLifecycle.CANDIDATE,
            SourceDefinition.origin.in_(AUTO_ACTIVATE_ORIGINS),
        )
    ).all()
    for source in candidates:
        if not activation_blockers(
            source, enforce_robots=robots_enforced(source, settings)
        ):
            source.lifecycle_status = SourceLifecycle.ACTIVE
            source.updated_at = utc_now()
            audit.record(
                session,
                "autopilot",
                "source.auto_activated",
                "source",
                source.id,
                {"name": source.name},
            )
            activated.append(source.name)
    return activated


def _maintenance_due(session: Session, settings: Settings) -> bool:
    meta = session.get(AppMeta, _MAINT_KEY)
    if meta is None:
        return True
    last = meta.value.get("at", "")
    cutoff = utc_now() - timedelta(hours=settings.autopilot_maintenance_hours)
    return last < cutoff.isoformat()


def autopilot_tick(
    session: Session, settings: Settings, transport: httpx.BaseTransport | None = None
) -> dict:
    # Refresh named regions from the DB so a separate-process worker sees GUI edits
    # within one tick (ADR-0012).
    load_regions(session)
    summary: dict = {"seeded": _bootstrap_seeds(session)}
    summary["policies_checked"] = _check_policies(session, settings, transport)
    summary["activated"] = _auto_activate(session, settings)
    summary["collection"] = collect_due(session, settings, transport=transport)

    if _maintenance_due(session, settings):
        summary["maintenance"] = evaluate_all(session)
        meta = session.get(AppMeta, _MAINT_KEY)
        stamp = {"at": utc_now().isoformat()}
        if meta is None:
            session.add(AppMeta(key=_MAINT_KEY, value=stamp))
        else:
            meta.value = stamp
            meta.updated_at = utc_now()
    return summary
