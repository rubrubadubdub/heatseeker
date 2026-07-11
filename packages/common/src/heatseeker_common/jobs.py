"""Job queue mechanics (spec §30): enqueue, claim, complete, retry, cancel.

Claiming uses a single atomic UPDATE ... RETURNING (SQLite is single-writer under WAL,
so the subselect-then-update cannot race between processes; busy_timeout serialises
writers). Callers own the session/transaction; the worker commits immediately after a
claim to hold it.
"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from heatseeker_common import audit
from heatseeker_common.models import Job, JobStatus, PriorityClass
from heatseeker_common.timeutil import utc_now

BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 300.0


def compute_backoff(attempts: int) -> float:
    """Exponential: 5s, 10s, 20s, ... capped at 5 minutes."""
    return min(BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_SECONDS)


def enqueue(
    session: Session,
    job_type: str,
    payload: dict | None = None,
    priority: int = PriorityClass.BACKGROUND_ENRICHMENT,
    max_attempts: int = 3,
    run_at: datetime | None = None,
    correlation_id: str | None = None,
    actor: str = "system",
) -> Job:
    job = Job(
        job_type=job_type,
        payload=payload or {},
        priority=int(priority),
        max_attempts=max_attempts,
        run_at=run_at or utc_now(),
        correlation_id=correlation_id,
    )
    session.add(job)
    session.flush()
    audit.record(
        session,
        actor,
        "job.enqueued",
        "job",
        job.id,
        {"job_type": job_type, "priority": int(priority)},
    )
    return job


def claim_next(session: Session, worker_id: str) -> Job | None:
    """Atomically claim the most urgent eligible job. Returns None when queue is idle."""
    now = utc_now()
    eligible = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED, Job.run_at <= now)
        .order_by(Job.priority, Job.run_at, Job.created_at)
        .limit(1)
        .scalar_subquery()
    )
    claimed = session.execute(
        update(Job)
        .where(Job.id == eligible, Job.status == JobStatus.QUEUED)
        .values(
            status=JobStatus.RUNNING,
            claimed_by=worker_id,
            started_at=now,
            heartbeat_at=now,
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
    ).scalar_one_or_none()
    if claimed is None:
        return None
    return session.get(Job, claimed)


def heartbeat(session: Session, job_id: str) -> None:
    session.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=utc_now()))


def mark_succeeded(session: Session, job_id: str, result: dict | None = None) -> Job:
    job = session.get(Job, job_id)
    assert job is not None
    job.status = JobStatus.SUCCEEDED
    job.result = result
    job.finished_at = utc_now()
    audit.record(session, job.claimed_by or "worker", "job.succeeded", "job", job.id)
    return job


def mark_failed(session: Session, job_id: str, error: str, retryable: bool = True) -> Job:
    """Requeue with backoff while attempts remain; otherwise final FAILED."""
    job = session.get(Job, job_id)
    assert job is not None
    job.error = error
    if retryable and job.attempts < job.max_attempts and not job.cancel_requested:
        job.status = JobStatus.QUEUED
        job.run_at = utc_now() + timedelta(seconds=compute_backoff(job.attempts))
        job.claimed_by = None
        action = "job.retry_scheduled"
    else:
        job.status = JobStatus.FAILED
        job.finished_at = utc_now()
        action = "job.failed"
    audit.record(
        session,
        job.claimed_by or "worker",
        action,
        "job",
        job.id,
        {"attempts": job.attempts, "error": error[:500]},
    )
    return job


def mark_cancelled(session: Session, job_id: str, reason: str = "cancelled") -> Job:
    job = session.get(Job, job_id)
    assert job is not None
    job.status = JobStatus.CANCELLED
    job.finished_at = utc_now()
    audit.record(
        session, job.claimed_by or "worker", "job.cancelled", "job", job.id, {"reason": reason}
    )
    return job


def reap_stale_jobs(session: Session, stale_after_seconds: float) -> int:
    """Requeue (or finally fail) RUNNING jobs whose worker heartbeat went silent.

    Covers worker crashes and kills: without this, a job stays RUNNING forever.
    Uses the normal retry path so max_attempts still bounds total executions.
    """
    cutoff = utc_now() - timedelta(seconds=stale_after_seconds)
    stale_ids = list(
        session.scalars(
            select(Job.id).where(
                Job.status == JobStatus.RUNNING,
                Job.heartbeat_at.is_not(None),
                Job.heartbeat_at < cutoff,
            )
        )
    )
    for job_id in stale_ids:
        mark_failed(session, job_id, "worker heartbeat lost — job reaped")
    return len(stale_ids)


def cancel(session: Session, job_id: str, actor: str = "user") -> bool:
    """Cancel a queued job immediately; flag a running job for cooperative cancel."""
    job = session.get(Job, job_id)
    if job is None:
        return False
    if job.status == JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.finished_at = utc_now()
    elif job.status == JobStatus.RUNNING:
        job.cancel_requested = True
    else:
        return False
    audit.record(session, actor, "job.cancel_requested", "job", job.id)
    return True
