"""One-click pipeline advancement: do the next right thing, report what happened.

The granular controls (per-source collect, per-document processing, manual scans,
per-entity refresh) all still exist — `advance()` just chains the same deterministic
steps in dependency order with bounded batch sizes, so a user doesn't have to know the
pipeline to keep it moving. Nothing here bypasses policy gates: collection stays behind
the existing autopilot/robots machinery, and nothing auto-merges.
"""

from heatseeker_common import jobs
from heatseeker_common.models import Job, PriorityClass
from heatseeker_common.settings import Settings
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

from heatseeker_intelligence import profile
from heatseeker_intelligence.models import FactAssertion, Observation

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
    session.flush()
    return summary
