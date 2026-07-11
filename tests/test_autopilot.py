"""Autopilot: the M2 funnel drives itself (seed -> policy -> activate -> collect -> maintain)."""

import httpx
from heatseeker_common.db import session_scope
from heatseeker_common.models import AuditLog
from heatseeker_source_registry.autopilot import autopilot_tick
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceLifecycle,
    TermsStatus,
)
from heatseeker_worker.runner import WorkerRunner
from sqlalchemy import select


def _ok_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(
            200, content=b"<html>content</html>", headers={"Content-Type": "text/html"}
        )

    return httpx.MockTransport(handler)


def test_autopilot_full_funnel_from_empty_registry(engine, settings):
    with session_scope(engine) as session:
        summary = autopilot_tick(session, settings, transport=_ok_transport())
        assert summary["seeded"] >= 20  # bootstrapped from packs, no clicks
        assert summary["policies_checked"] > 0  # bounded polite batch
        assert summary["activated"]  # cleared candidates auto-activated
        assert "maintenance" in summary  # first tick grades everything

    # Second tick: no reseeding, activation continues, maintenance gated by interval.
    with session_scope(engine) as session:
        summary2 = autopilot_tick(session, settings, transport=_ok_transport())
        assert summary2["seeded"] == 0
        assert "maintenance" not in summary2


def test_autopilot_never_activates_blocked_or_proposals(engine, settings):
    settings.robots_policy = "enforce"
    with session_scope(engine) as session:
        session.add(
            SourceDefinition(
                name="Disallowed",
                source_category="news",
                base_url="https://x.example/",
                access_method="html",
                lifecycle_status=SourceLifecycle.CANDIDATE,
                robots_status=RobotsStatus.DISALLOWED,
                terms_status=TermsStatus.APPROVED,
            )
        )
        session.add(
            SourceDefinition(
                name="AI proposal",
                source_category="news",
                base_url="https://y.example/",
                access_method="html",
                lifecycle_status=SourceLifecycle.CANDIDATE,
                robots_status=RobotsStatus.ALLOWED,
                origin="proposal",
            )
        )
    with session_scope(engine) as session:
        summary = autopilot_tick(session, settings, transport=_ok_transport())
        assert "Disallowed" not in summary["activated"]
        assert "AI proposal" not in summary["activated"]
    with session_scope(engine) as session:
        rows = {s.name: s.lifecycle_status for s in session.scalars(select(SourceDefinition))}
        assert rows["AI proposal"] == SourceLifecycle.CANDIDATE  # waits for review funnel


def test_autopilot_actions_are_audited(engine, settings):
    with session_scope(engine) as session:
        autopilot_tick(session, settings, transport=_ok_transport())
    with session_scope(engine) as session:
        actions = set(session.scalars(select(AuditLog.action)))
        assert "source.auto_activated" in actions
        assert "sources.evaluated" in actions


def test_worker_enqueues_autopilot_only_once(engine, settings):
    runner = WorkerRunner(settings, worker_id="ap-test")
    try:
        assert runner.maybe_enqueue_autopilot() is True
        assert runner.maybe_enqueue_autopilot() is False  # one pending tick at a time
    finally:
        runner.engine.dispose()


def test_worker_autopilot_respects_disable_flag(engine, settings):
    settings.autopilot_enabled = False
    runner = WorkerRunner(settings, worker_id="ap-off")
    try:
        assert runner.maybe_enqueue_autopilot() is False
    finally:
        runner.engine.dispose()
