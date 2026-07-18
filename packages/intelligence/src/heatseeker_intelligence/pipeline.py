"""One-click pipeline advancement: do the next right thing, report what happened.

The granular controls (per-source collect, per-document processing, manual scans,
per-entity refresh) all still exist — `advance()` just chains the same deterministic
steps in dependency order with bounded batch sizes, so a user doesn't have to know the
pipeline to keep it moving. Nothing here bypasses policy gates: collection stays behind
the existing autopilot/robots machinery, and nothing auto-merges.
"""

from datetime import datetime, timedelta

from heatseeker_common import jobs
from heatseeker_common.models import Job, PriorityClass
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.matching import scan_for_duplicates
from heatseeker_entity_resolution.models import Organisation, OrganisationStatus
from heatseeker_entity_resolution.resolution import canonical_id
from heatseeker_source_registry.document_pipeline import (
    enqueue_document_processing,
    processing_config,
)
from heatseeker_source_registry.document_processing import PROCESSOR_VERSION
from heatseeker_source_registry.models import DocumentProcessingRun, SourceDocument
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from heatseeker_intelligence import confidence, profile
from heatseeker_intelligence.capabilities import HISTORICAL_AFTER_DAYS
from heatseeker_intelligence.models import (
    CapabilityAssignment,
    CapabilityStatus,
    FactAssertion,
    Observation,
)

PIPELINE_VERSION = "pipeline/0.1"


def _queue_collection_tick(session: Session, actor: str) -> bool:
    """Ask for one autopilot collection pass unless one is already pending."""
    pending = session.scalars(
        select(Job.id).where(
            Job.job_type == "sources.autopilot", Job.status.in_(["queued", "running"])
        )
    ).first()
    if pending is not None:
        return False
    jobs.enqueue(
        session,
        "sources.autopilot",
        priority=PriorityClass.SCHEDULED_PRIORITY,
        actor=actor,
    )
    return True


def _queue_document_backlog(
    session: Session, settings: Settings, actor: str, limit: int
) -> int:
    """Queue processing for evidence documents that lack a current-version run."""
    _limits, config_hash, _config = processing_config(settings)
    processed = select(DocumentProcessingRun.source_document_id).where(
        DocumentProcessingRun.pipeline_version == PROCESSOR_VERSION,
        DocumentProcessingRun.config_hash == config_hash,
    )
    backlog = session.scalars(
        select(SourceDocument)
        .where(SourceDocument.id.not_in(processed))
        .order_by(SourceDocument.retrieved_at.desc())
        .limit(limit)
    ).all()
    queued = 0
    for document in backlog:
        if enqueue_document_processing(session, settings, document, actor=actor) is not None:
            queued += 1
    return queued


def _refresh_stale_entities(session: Session, limit: int) -> int:
    """Reconcile facts/sizes/gaps where evidence is newer than the conclusions."""
    newest_observation = dict(
        session.execute(
            select(Observation.subject_entity_id, func.max(Observation.observed_at))
            .where(Observation.subject_entity_id.is_not(None))
            .group_by(Observation.subject_entity_id)
        ).all()
    )
    if not newest_observation:
        return 0
    newest_assertion = dict(
        session.execute(
            select(FactAssertion.subject_entity_id, func.max(FactAssertion.updated_at))
            .group_by(FactAssertion.subject_entity_id)
        ).all()
    )
    stale_canonicals: dict[str, None] = {}
    for entity_id, observed in newest_observation.items():
        canonical = canonical_id(session, entity_id)
        asserted = newest_assertion.get(canonical)
        if asserted is None or observed > asserted:
            stale_canonicals.setdefault(canonical, None)
        if len(stale_canonicals) >= limit:
            break
    if len(stale_canonicals) < limit:
        assertions = session.scalars(select(FactAssertion).order_by(FactAssertion.updated_at)).all()
        for assertion in assertions:
            current_freshness = confidence.freshness_score(
                assertion.predicate, assertion.last_observed_at
            )
            if abs(current_freshness - assertion.freshness_score) < 0.01:
                continue
            stale_canonicals.setdefault(
                canonical_id(session, assertion.subject_entity_id), None
            )
            if len(stale_canonicals) >= limit:
                break
    if len(stale_canonicals) < limit:
        historical_cutoff = utc_now() - timedelta(days=HISTORICAL_AFTER_DAYS)
        ageing_capabilities = session.scalars(
            select(CapabilityAssignment).where(
                CapabilityAssignment.capability_status.not_in(
                    [CapabilityStatus.VERIFIED, CapabilityStatus.HISTORICAL]
                )
            )
        ).all()
        for capability in ageing_capabilities:
            observed_times = [
                datetime.fromisoformat(entry["observed_at"])
                for entry in capability.evidence_ids or []
                if isinstance(entry, dict) and entry.get("observed_at")
            ]
            if observed_times and max(observed_times) >= historical_cutoff:
                continue
            stale_canonicals.setdefault(
                canonical_id(session, capability.organisation_id), None
            )
            if len(stale_canonicals) >= limit:
                break
    for organisation_id in stale_canonicals:
        profile.refresh(session, organisation_id)
    return len(stale_canonicals)


def advance(
    session: Session,
    settings: Settings,
    *,
    actor: str = "user",
    max_documents: int = 500,
    max_entities: int = 200,
) -> dict:
    """Advance every pipeline stage that has work waiting; return what was done."""
    summary: dict = {"pipeline_version": PIPELINE_VERSION}

    summary["collection_tick_queued"] = _queue_collection_tick(session, actor)
    summary["documents_queued"] = _queue_document_backlog(
        session, settings, actor, max_documents
    )

    live_organisations = session.execute(
        select(func.count(Organisation.id)).where(
            Organisation.status != OrganisationStatus.MERGED
        )
    ).scalar_one()
    if live_organisations >= 2:
        scan = scan_for_duplicates(session)
        summary["match_scan"] = {
            "pairs_scored": scan["pairs_scored"],
            "candidates_created": scan["candidates_created"],
            "candidates_updated": scan["candidates_updated"],
        }
    else:
        summary["match_scan"] = None

    summary["entities_refreshed"] = _refresh_stale_entities(session, max_entities)
    summary["profile_fetches_queued"] = _queue_profile_fetches(session, actor)
    summary["lead_rescores_queued"] = _queue_lead_rescores(session, actor)
    session.flush()
    return summary


def _queue_profile_fetches(session: Session, actor: str, limit: int = 25) -> int:
    """Queue deterministic website profile fetches for organisations that have a
    domain but no public contact route yet — the AI-free enrichment loop (§41.19)."""
    from heatseeker_entity_resolution.models import ContactPoint, OrganisationDomain

    with_domain = select(OrganisationDomain.organisation_id).distinct()
    with_contact = select(ContactPoint.organisation_id).distinct()
    candidates = session.scalars(
        select(Organisation.id)
        .where(
            Organisation.status != OrganisationStatus.MERGED,
            Organisation.id.in_(with_domain),
            Organisation.id.not_in(with_contact),
        )
        .limit(limit * 3)
    ).all()
    queued = 0
    for organisation_id in candidates:
        if queued >= limit:
            break
        pending = session.scalars(
            select(Job.id).where(
                Job.job_type == "profiles.fetch",
                Job.status.in_(["queued", "running", "succeeded"]),
                Job.payload["organisation_id"].as_string() == organisation_id,
            )
        ).first()
        if pending is None:
            jobs.enqueue(
                session,
                "profiles.fetch",
                payload={"organisation_id": organisation_id},
                priority=PriorityClass.BACKGROUND_ENRICHMENT,
                actor=actor,
            )
            queued += 1
    return queued


def _queue_lead_rescores(session: Session, actor: str) -> int:
    """Queue a rescore per active offering (idempotent; skips already-queued ones).

    Imported lazily: lead intelligence sits above this package in the layer stack,
    and the pipeline must keep working if M8 tables aren't migrated yet.
    """
    try:
        from heatseeker_lead_intelligence.models import Offering, OfferingStatus
    except ImportError:  # pragma: no cover — package always present in the workspace
        return 0
    queued = 0
    offerings = session.execute(
        select(Offering.id).where(Offering.status == OfferingStatus.ACTIVE)
    ).scalars()
    for offering_id in offerings:
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
