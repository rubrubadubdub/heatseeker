from datetime import timedelta

from heatseeker_common import jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import AuditLog, Job, JobStatus, PriorityClass
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select


def test_enqueue_claim_complete(engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo", payload={"x": 1}, actor="test")
        job_id = job.id

    with session_scope(engine) as session:
        claimed = jobs.claim_next(session, "worker-1")
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == JobStatus.RUNNING
        assert claimed.attempts == 1
        assert claimed.claimed_by == "worker-1"

    with session_scope(engine) as session:
        jobs.mark_succeeded(session, job_id, {"ok": True})

    with session_scope(engine) as session:
        done = session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.result == {"ok": True}
        assert done.finished_at is not None


def test_priority_ordering(engine):
    with session_scope(engine) as session:
        jobs.enqueue(session, "low", priority=PriorityClass.MAINTENANCE)
        urgent = jobs.enqueue(session, "urgent", priority=PriorityClass.INTERACTIVE)
        urgent_id = urgent.id

    with session_scope(engine) as session:
        claimed = jobs.claim_next(session, "w")
        assert claimed.id == urgent_id


def test_future_run_at_not_claimed(engine):
    with session_scope(engine) as session:
        jobs.enqueue(session, "later", run_at=utc_now() + timedelta(hours=1))

    with session_scope(engine) as session:
        assert jobs.claim_next(session, "w") is None


def test_failure_requeues_with_backoff_then_fails_finally(engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "flaky", max_attempts=2)
        job_id = job.id

    # attempt 1: fails -> requeued in the future
    with session_scope(engine) as session:
        jobs.claim_next(session, "w")
    with session_scope(engine) as session:
        jobs.mark_failed(session, job_id, "boom")
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.QUEUED
        assert job.run_at > utc_now()
        # make eligible immediately for the next attempt
        job.run_at = utc_now()

    # attempt 2 (== max_attempts): fails -> final FAILED
    with session_scope(engine) as session:
        claimed = jobs.claim_next(session, "w")
        assert claimed.attempts == 2
    with session_scope(engine) as session:
        jobs.mark_failed(session, job_id, "boom again")
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.FAILED
        assert job.finished_at is not None


def test_backoff_growth_and_cap():
    assert jobs.compute_backoff(1) == 5.0
    assert jobs.compute_backoff(2) == 10.0
    assert jobs.compute_backoff(3) == 20.0
    assert jobs.compute_backoff(50) == jobs.BACKOFF_CAP_SECONDS


def test_cancel_queued_job(engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "doomed")
        job_id = job.id
    with session_scope(engine) as session:
        assert jobs.cancel(session, job_id, actor="test") is True
    with session_scope(engine) as session:
        assert session.get(Job, job_id).status == JobStatus.CANCELLED
        assert jobs.claim_next(session, "w") is None


def test_audit_trail_written(engine):
    with session_scope(engine) as session:
        jobs.enqueue(session, "demo.echo", actor="test")
    with session_scope(engine) as session:
        actions = session.scalars(select(AuditLog.action)).all()
        assert "job.enqueued" in actions
