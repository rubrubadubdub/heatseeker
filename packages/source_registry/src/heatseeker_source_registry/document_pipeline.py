"""Versioned persistence and job scheduling for derived document evidence.

Raw SourceDocument bytes remain immutable. Each processor/config combination creates one
append-only run and versioned artifacts; parser failures never affect source fetch health.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from heatseeker_common import audit, jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, JobStatus, PriorityClass
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from heatseeker_source_registry import rawstore
from heatseeker_source_registry.document_processing import (
    PROCESSOR_VERSION,
    ProcessingLimits,
    process_bytes,
    read_processed_output,
    write_processed_output,
)
from heatseeker_source_registry.models import DocumentProcessingRun, SourceDocument


def processing_limits(settings: Settings) -> ProcessingLimits:
    return ProcessingLimits(
        max_input_bytes=max(
            settings.fetch_document_max_bytes,
            settings.fetch_image_max_bytes,
            settings.evidence_upload_max_bytes,
        ),
        max_text_chars=settings.document_max_extracted_chars,
        max_pages=settings.document_max_pages,
        max_zip_entries=settings.document_zip_max_entries,
        max_zip_uncompressed_bytes=settings.document_zip_max_uncompressed_bytes,
        max_zip_compression_ratio=settings.document_zip_max_ratio,
        max_image_pixels=settings.image_max_pixels,
        max_image_frames=settings.image_max_frames,
    )


def processing_config(settings: Settings) -> tuple[ProcessingLimits, str, dict]:
    limits = processing_limits(settings)
    config = {
        "limits": asdict(limits),
        "ocr_enabled": settings.evidence_ocr_enabled,
        "vision_enabled": settings.evidence_vision_enabled,
    }
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return limits, hashlib.sha256(encoded).hexdigest(), config


def latest_processing_run(session: Session, document_id: str) -> DocumentProcessingRun | None:
    return session.scalars(
        select(DocumentProcessingRun)
        .where(DocumentProcessingRun.source_document_id == document_id)
        .order_by(DocumentProcessingRun.created_at.desc())
        .limit(1)
    ).first()


def enqueue_document_processing(
    session: Session,
    settings: Settings,
    document: SourceDocument,
    *,
    actor: str = "collector",
    priority: int = PriorityClass.BACKGROUND_ENRICHMENT,
) -> Job | None:
    """Queue one idempotent processing job unless this version is done or pending."""
    _limits, config_hash, _config = processing_config(settings)
    existing = session.scalars(
        select(DocumentProcessingRun.id).where(
            DocumentProcessingRun.source_document_id == document.id,
            DocumentProcessingRun.pipeline_version == PROCESSOR_VERSION,
            DocumentProcessingRun.config_hash == config_hash,
        )
    ).first()
    if existing is not None:
        return None
    pending = session.scalars(
        select(Job).where(
            Job.job_type == "documents.process",
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    )
    if any(job.payload.get("document_id") == document.id for job in pending):
        return None
    return jobs.enqueue(
        session,
        "documents.process",
        payload={"schema_version": 1, "document_id": document.id},
        priority=priority,
        max_attempts=2,
        actor=actor,
    )


def _artifact_version(config_hash: str) -> str:
    return f"{PROCESSOR_VERSION}+{config_hash[:16]}"


def process_document(engine: Engine, settings: Settings, document_id: str) -> dict:
    """Process one document outside a long DB transaction, then publish metadata."""
    limits, config_hash, config = processing_config(settings)
    with session_scope(engine) as session:
        existing = session.scalars(
            select(DocumentProcessingRun).where(
                DocumentProcessingRun.source_document_id == document_id,
                DocumentProcessingRun.pipeline_version == PROCESSOR_VERSION,
                DocumentProcessingRun.config_hash == config_hash,
            )
        ).first()
        if existing is not None:
            return {
                "document_id": document_id,
                "processing_run_id": existing.id,
                "status": existing.status,
                "deduplicated": True,
            }
        document = session.get(SourceDocument, document_id)
        if document is None:
            raise LookupError(f"document not found: {document_id}")
        snapshot = {
            "raw_storage_path": document.raw_storage_path,
            "content_hash": document.content_hash,
            "content_type": document.content_type,
            "original_filename": document.original_filename,
        }

    try:
        raw = rawstore.read_bytes(settings, snapshot["raw_storage_path"])
    except FileNotFoundError:
        result = None
        status = "failed"
        error = "raw_missing: immutable raw evidence is missing from storage"
        manifest = {
            "schema_version": 1,
            "processor_version": PROCESSOR_VERSION,
            "input_sha256": snapshot["content_hash"],
            "status": status,
            "error_code": "raw_missing",
            "error_detail": error,
        }
        text = None
    else:
        result = process_bytes(
            raw,
            declared_type=snapshot["content_type"],
            filename=snapshot["original_filename"],
            limits=limits,
        )
        status = result.status
        error = (
            f"{result.error_code}: {result.error_detail}"
            if result.error_code and result.error_detail
            else result.error_code or result.error_detail
        )
        manifest = dict(result.manifest)
        manifest["processing_config"] = config
        manifest["analysis_capabilities"] = {
            "ocr": "unavailable" if settings.evidence_ocr_enabled else "disabled",
            "vision": "unavailable" if settings.evidence_vision_enabled else "disabled",
        }
        text = result.text

    artifact_version = _artifact_version(config_hash)
    manifest_path = write_processed_output(
        settings.processed_dir,
        snapshot["content_hash"],
        artifact_version,
        manifest,
        filename="manifest.json",
    )
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    text_path = None
    text_hash = None
    if text:
        text_path = write_processed_output(
            settings.processed_dir,
            snapshot["content_hash"],
            artifact_version,
            text,
            filename="text.txt",
        )
        text_hash = hashlib.sha256(text.encode()).hexdigest()

    with session_scope(engine) as session:
        document = session.get(SourceDocument, document_id)
        if document is None:
            raise LookupError(f"document disappeared during processing: {document_id}")
        existing = session.scalars(
            select(DocumentProcessingRun).where(
                DocumentProcessingRun.source_document_id == document_id,
                DocumentProcessingRun.pipeline_version == PROCESSOR_VERSION,
                DocumentProcessingRun.config_hash == config_hash,
            )
        ).first()
        if existing is not None:
            return {
                "document_id": document_id,
                "processing_run_id": existing.id,
                "status": existing.status,
                "deduplicated": True,
            }
        now = utc_now()
        run = DocumentProcessingRun(
            source_document_id=document_id,
            pipeline_version=PROCESSOR_VERSION,
            config_hash=config_hash,
            status=status,
            detected_content_type=(
                result.detected_content_type if result is not None else document.content_type
            ),
            filename=document.original_filename,
            extraction_method=result.extraction_method if result is not None else "none",
            manifest_path=manifest_path,
            manifest_hash=hashlib.sha256(manifest_bytes).hexdigest(),
            text_path=text_path,
            text_hash=text_hash,
            text_chars=len(text) if text else 0,
            page_count=result.page_count if result is not None else None,
            processing_metadata=result.metadata if result is not None else {},
            warnings=result.warnings if result is not None else [],
            error=error,
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        session.flush()
        document.detected_content_type = run.detected_content_type
        if text_path:
            document.distilled_path = text_path
            document.distilled_chars = len(text)
            document.parser_version = PROCESSOR_VERSION
        audit.record(
            session,
            "document-processor",
            "document.processed",
            "source_document",
            document.id,
            {
                "run_id": run.id,
                "status": status,
                "detected_content_type": run.detected_content_type,
                "text_chars": run.text_chars,
                "page_count": run.page_count,
            },
        )
        run_id = run.id
    return {
        "document_id": document_id,
        "processing_run_id": run_id,
        "status": status,
        "detected_content_type": (
            result.detected_content_type if result is not None else snapshot["content_type"]
        ),
        "text_chars": len(text) if text else 0,
        "deduplicated": False,
    }


def read_run_text(settings: Settings, run: DocumentProcessingRun) -> str | None:
    if not run.text_path:
        return None
    return read_processed_output(settings.processed_dir, run.text_path).decode("utf-8")
