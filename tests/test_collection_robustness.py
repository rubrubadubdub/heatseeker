"""Grading, auto-deprecation, throttle compliance, cadence, storage/token economy."""

import gzip
from datetime import timedelta

import httpx
import pytest
from heatseeker_common.db import session_scope
from heatseeker_common.timeutil import utc_now
from heatseeker_source_registry import rawstore
from heatseeker_source_registry.collect import collect_source
from heatseeker_source_registry.distill import html_to_text, read_distilled
from heatseeker_source_registry.grading import (
    compute_grade,
    evaluate_all,
    reinstate,
)
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
    TermsStatus,
)
from heatseeker_source_registry.schedule import collect_due, update_cadence


def _source(session, **overrides) -> SourceDefinition:
    defaults = dict(
        name=f"Src {overrides.get('name', 'X')}",
        source_category="news",
        base_url="https://example.org/page",
        access_method="html",
        authority_tier=3,
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.ALLOWED,
        terms_status=TermsStatus.APPROVED,
    )
    defaults.update(overrides)
    row = SourceDefinition(**defaults)
    session.add(row)
    session.flush()
    return row


def test_grade_abstains_without_history(engine):
    with session_scope(engine) as session:
        source = _source(session)
        score, letter, detail = compute_grade(source)
        assert score is None and letter == "U"
        assert "abstain" in detail["reason"]


def test_grade_rewards_reliable_novel_official_sources(engine):
    with session_scope(engine) as session:
        good = _source(
            session,
            name="good",
            authority_tier=1,
            fetch_attempts=10,
            fetch_successes=10,
            docs_new=8,
            docs_unchanged=2,
        )
        poor = _source(
            session,
            name="poor",
            authority_tier=7,
            robots_status=RobotsStatus.UNKNOWN,
            terms_status=TermsStatus.UNCLEAR,
            fetch_attempts=10,
            fetch_successes=3,
            docs_new=0,
            docs_unchanged=3,
            consecutive_failures=4,
        )
        good_score, good_letter, _ = compute_grade(good)
        poor_score, poor_letter, _ = compute_grade(poor)
        assert good_letter in ("A", "B") and good_score > 70
        assert poor_letter in ("D", "E") and poor_score < poor_score + 1  # sanity
        assert good_score > poor_score + 30


def test_auto_deprecation_and_reinstate(engine):
    with session_scope(engine) as session:
        failing = _source(
            session,
            name="dead",
            fetch_attempts=10,
            fetch_successes=1,
            consecutive_failures=9,
            last_success_at=utc_now() - timedelta(days=60),
        )
        prohibited = _source(
            session,
            name="banned",
            terms_status=TermsStatus.PROHIBITED,
            fetch_attempts=3,
            fetch_successes=3,
        )
        healthy = _source(
            session,
            name="fine",
            fetch_attempts=5,
            fetch_successes=5,
            docs_new=4,
            docs_unchanged=1,
            authority_tier=1,
        )
        failing_id, prohibited_id, healthy_id = failing.id, prohibited.id, healthy.id

        summary = evaluate_all(session)
        assert len(summary["deprecated"]) == 2

    with session_scope(engine) as session:
        assert session.get(SourceDefinition, failing_id).lifecycle_status == "deprecated"
        assert (
            "persistently failing" in session.get(SourceDefinition, failing_id).deprecation_reason
        )
        assert session.get(SourceDefinition, prohibited_id).lifecycle_status == "deprecated"
        assert session.get(SourceDefinition, healthy_id).lifecycle_status == "active"
        assert session.get(SourceDefinition, healthy_id).quality_grade in ("A", "B")

        restored = reinstate(session, failing_id, actor="test")
        assert restored.lifecycle_status == "candidate"
        assert restored.deprecation_reason is None


def test_throttle_sets_retry_after_without_degrading(engine, settings):
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with session_scope(engine) as session:
        source = _source(session, name="busy")
        source_id = source.id
    with session_scope(engine) as session:
        result = collect_source(session, settings, source_id, transport=transport)
        assert result["outcome"] == "throttled"
        assert result["retry_after_seconds"] == 120.0
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        assert source.retry_after_until > utc_now()
        assert source.consecutive_failures == 0  # not a defect
        assert source.lifecycle_status == "active"


def test_cadence_adapts_to_change_rate(engine):
    with session_scope(engine) as session:
        source = _source(session, name="cadence", collect_interval_seconds=86400.0)
        update_cadence(source, "collected")
        assert source.collect_interval_seconds == pytest.approx(86400 * 0.6)
        for _ in range(20):
            update_cadence(source, "unchanged")
        assert source.collect_interval_seconds == 7 * 86400.0  # capped at weekly
        assert source.next_collect_at > utc_now()


def test_collect_due_respects_schedule_and_throttle(engine, settings):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"payload", headers={"Content-Type": "text/html"})
    )
    delays: list[float] = []
    with session_scope(engine) as session:
        _source(session, name="due-now", base_url="https://a.example/x")
        _source(
            session,
            name="not-due",
            base_url="https://b.example/x",
            next_collect_at=utc_now() + timedelta(hours=5),
        )
        _source(
            session,
            name="throttled",
            base_url="https://c.example/x",
            retry_after_until=utc_now() + timedelta(minutes=10),
        )
        result = collect_due(session, settings, transport=transport, sleeper=delays.append)
        assert result["due"] == 1
        assert list(result["outcomes"].values()) == ["collected"]


def test_rawstore_gzip_roundtrip(settings):
    settings.ensure_data_dirs()
    body = b"<html>" + b"scaffolding " * 200 + b"</html>"
    rel, digest = rawstore.store_bytes(settings, body, "text/html")
    assert rel.endswith(".gz")
    assert (settings.raw_dir / rel).stat().st_size < len(body)  # actually compressed
    assert rawstore.read_bytes(settings, rel) == body  # transparent decompression
    assert digest == rawstore.content_address(body)  # hash of original bytes


def test_distillation_produces_token_lean_text(engine, settings):
    page = (
        b"<html><head><title>Acme Wins Tender</title><style>.x{color:red}</style></head>"
        b"<body><nav>Home | About</nav><script>track()</script>"
        b"<article><h1>Acme Scaffolding wins major rail tender</h1>"
        b"<p>The contract covers access design across Queensland.</p></article>"
        b"<footer>Copyright</footer></body></html>"
    )
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=page, headers={"Content-Type": "text/html"})
    )
    with session_scope(engine) as session:
        source = _source(session, name="distil")
        result = collect_source(session, settings, source.id, transport=transport)
        document = session.get(SourceDocument, result["document_id"])
        assert document.distilled_chars is not None
        text = read_distilled(settings, document)
        assert "Acme Scaffolding wins major rail tender" in text
        assert "track()" not in text and "color:red" not in text  # boilerplate gone
        assert document.distilled_chars < len(page)


def test_html_to_text_handles_plain_fragment():
    assert "hello world" in html_to_text(b"<p>hello   world</p>")


def test_distilled_storage_is_compressed(engine, settings):
    body = b"<html><body>" + b"<p>repeat scaffold text</p>" * 100 + b"</body></html>"
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=body, headers={"Content-Type": "text/html"})
    )
    with session_scope(engine) as session:
        source = _source(session, name="gzip-distil")
        result = collect_source(session, settings, source.id, transport=transport)
        document = session.get(SourceDocument, result["document_id"])
        stored = settings.processed_dir / document.distilled_path
        assert stored.suffix == ".gz"
        assert len(gzip.decompress(stored.read_bytes())) == document.distilled_chars


def test_raw_store_deduplicates_across_mime_variants(settings):
    settings.ensure_data_dirs()
    body = b"same evidence bytes " * 100
    first, digest = rawstore.store_bytes(settings, body, "text/plain")
    second, second_digest = rawstore.store_bytes(settings, body, "application/octet-stream")

    assert second == first
    assert second_digest == digest
    assert rawstore.read_bytes(settings, first) == body
    stored = [path for path in settings.raw_dir.rglob("*") if path.is_file()]
    assert len(stored) == 1
