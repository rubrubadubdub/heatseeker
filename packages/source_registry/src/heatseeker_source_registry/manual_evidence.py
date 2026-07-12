"""Manual file evidence capture with the same immutable provenance as fetched bytes."""

from heatseeker_common import audit
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry import COLLECTOR_VERSION
from heatseeker_source_registry.document_pipeline import enqueue_document_processing
from heatseeker_source_registry.document_processing import detect_media_type
from heatseeker_source_registry.models import SourceDefinition, SourceDocument
from heatseeker_source_registry.rawstore import store_bytes


def add_manual_file(
    session: Session,
    settings: Settings,
    source: SourceDefinition,
    content: bytes,
    *,
    filename: str,
    content_type: str | None,
    actor: str,
) -> tuple[SourceDocument, bool]:
    if not content:
        raise ValueError("uploaded evidence is empty")
    if len(content) > settings.evidence_upload_max_bytes:
        raise ValueError(f"uploaded evidence exceeds {settings.evidence_upload_max_bytes} bytes")
    safe_filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()[:500]
    if not safe_filename:
        safe_filename = "evidence.bin"
    rel_path, digest = store_bytes(settings, content, content_type)
    source_url = f"manual://upload/{source.id}/{digest}"
    existing = session.scalars(
        select(SourceDocument).where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.source_url == source_url,
            SourceDocument.content_hash == digest,
        )
    ).first()
    if existing is not None:
        existing.last_seen_at = utc_now()
        existing.retrieval_count += 1
        enqueue_document_processing(session, settings, existing, actor=actor)
        return existing, False

    document = SourceDocument(
        source_definition_id=source.id,
        source_url=source_url,
        content_hash=digest,
        content_type=content_type,
        detected_content_type=detect_media_type(content, content_type, safe_filename),
        original_filename=safe_filename,
        size_bytes=len(content),
        raw_storage_path=rel_path,
        access_policy_snapshot={
            "acquisition": "manual_upload",
            "actor": actor,
            "robots_status": "not_applicable",
            "robots_enforced": False,
            "terms_status": str(source.terms_status),
        },
        targeting_snapshot={
            "schema_version": 1,
            "mode": "manual_upload",
            "coverage_ids": [],
            "coverages": [],
            "research_scopes": [],
        },
        collector_version=f"{COLLECTOR_VERSION}/manual",
    )
    session.add(document)
    session.flush()
    enqueue_document_processing(session, settings, document, actor=actor)
    audit.record(
        session,
        actor,
        "document.uploaded",
        "source_document",
        document.id,
        {
            "source_id": source.id,
            "filename": safe_filename,
            "bytes": len(content),
            "hash": digest[:12],
        },
    )
    return document, True
