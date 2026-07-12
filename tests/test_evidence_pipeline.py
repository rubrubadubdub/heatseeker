"""End-to-end non-HTML evidence capture, processing, delivery, and failure isolation."""

import io

from fastapi.testclient import TestClient
from heatseeker_api.main import create_app
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job
from heatseeker_source_registry import rawstore
from heatseeker_source_registry.document_pipeline import process_document
from heatseeker_source_registry.manual_evidence import add_manual_file
from heatseeker_source_registry.models import (
    DocumentProcessingRun,
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
    TermsStatus,
)
from heatseeker_worker.runner import run_worker
from PIL import Image
from sqlalchemy import func, select


def _source(session) -> SourceDefinition:
    source = SourceDefinition(
        name="Evidence source",
        source_category="first_party",
        base_url="https://evidence.example/",
        access_method="html",
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.ALLOWED,
        terms_status=TermsStatus.APPROVED,
    )
    session.add(source)
    session.flush()
    return source


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color=(20, 80, 140)).save(output, format="PNG")
    return output.getvalue()


def test_manual_upload_processes_text_and_serves_original_safely(engine, settings):
    settings.autopilot_enabled = False
    with session_scope(engine) as session:
        source_id = _source(session).id

    client = TestClient(create_app(settings))
    uploaded = client.post(
        f"/api/sources/{source_id}/documents",
        files={"file": ("capability.txt", b"Industrial access capability", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document_id"]

    pending = client.get(f"/api/documents/{document_id}/text")
    assert pending.status_code == 409
    assert run_worker(settings, once=True, worker_id="document-test") == 1

    extracted = client.get(f"/api/documents/{document_id}/text")
    assert extracted.status_code == 200
    assert extracted.json()["text"] == "Industrial access capability"
    detail = client.get(f"/api/documents/{document_id}").json()
    assert detail["detected_content_type"] == "text/plain"
    assert detail["processing_runs"][0]["status"] == "succeeded"
    assert detail["processing_runs"][0]["extraction_method"] == "native"
    manifest = client.get(f"/api/documents/{document_id}/manifest")
    assert manifest.status_code == 200
    assert manifest.json()["segments"][0]["location"] == {"scope": "document"}

    original = client.get(f"/api/documents/{document_id}/raw")
    assert original.content == b"Industrial access capability"
    assert original.headers["x-content-type-options"] == "nosniff"
    assert original.headers["content-type"].startswith("application/octet-stream")
    assert original.headers["content-disposition"].startswith("attachment")


def test_image_metadata_and_safe_inline_preview(engine, settings):
    with session_scope(engine) as session:
        source = _source(session)
        document, _created = add_manual_file(
            session,
            settings,
            source,
            _png(),
            filename="project.png",
            content_type="application/octet-stream",
            actor="test",
        )
        document_id = document.id

    result = process_document(engine, settings, document_id)
    assert result["status"] == "succeeded"
    with session_scope(engine) as session:
        run = session.scalars(select(DocumentProcessingRun)).one()
        assert run.detected_content_type == "image/png"
        assert run.extraction_method == "metadata_only"
        assert run.processing_metadata["width"] == 8
        assert run.processing_metadata["height"] == 6
        assert run.text_path is None

    client = TestClient(create_app(settings))
    preview = client.get(f"/api/documents/{document_id}/raw", params={"inline": True})
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.headers["content-disposition"].startswith("inline")


def test_corrupt_pdf_is_retained_and_does_not_degrade_source(engine, settings):
    raw = b"%PDF-1.7\nthis is intentionally truncated"
    with session_scope(engine) as session:
        source = _source(session)
        source_id = source.id
        document, _created = add_manual_file(
            session,
            settings,
            source,
            raw,
            filename="broken.pdf",
            content_type="application/pdf",
            actor="test",
        )
        document_id = document.id

    result = process_document(engine, settings, document_id)
    assert result["status"] == "corrupt"
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        document = session.get(SourceDocument, document_id)
        run = session.scalars(select(DocumentProcessingRun)).one()
        assert source.consecutive_failures == 0
        assert source.last_failure_at is None
        assert run.status == "corrupt"
        assert run.error
        assert rawstore.read_bytes(settings, document.raw_storage_path) == raw


def test_processing_is_idempotent_and_get_text_does_not_write(engine, settings):
    with session_scope(engine) as session:
        source = _source(session)
        document, _created = add_manual_file(
            session,
            settings,
            source,
            b"repeatable evidence",
            filename="repeat.txt",
            content_type="text/plain",
            actor="test",
        )
        document_id = document.id
    first = process_document(engine, settings, document_id)
    second = process_document(engine, settings, document_id)
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True

    client = TestClient(create_app(settings))
    with session_scope(engine) as session:
        before_runs = session.scalar(select(func.count(DocumentProcessingRun.id)))
        before_jobs = session.scalar(select(func.count(Job.id)))
    assert client.get(f"/api/documents/{document_id}/text").status_code == 200
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(DocumentProcessingRun.id))) == before_runs
        assert session.scalar(select(func.count(Job.id))) == before_jobs


def test_ui_upload_and_evidence_page_show_processing_state(engine, settings):
    with session_scope(engine) as session:
        source_id = _source(session).id
    client = TestClient(create_app(settings))
    response = client.post(
        f"/sources/{source_id}/evidence/upload",
        files={"file": ("brief.txt", b"Evidence briefing", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/evidence/")
    detail = client.get(location)
    assert detail.status_code == 200
    assert "Download immutable original" in detail.text
    assert "pending" in detail.text
    listing = client.get("/evidence")
    assert "brief.txt" in listing.text
    assert "pending" in listing.text
    capabilities = client.get("/api/evidence/capabilities").json()
    assert capabilities["ocr"]["status"] == "disabled"
    assert capabilities["vision"]["available"] is False
