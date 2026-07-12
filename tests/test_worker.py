from heatseeker_common import jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, JobStatus, WorkerRegistration
from heatseeker_common.timeutil import utc_now
from heatseeker_worker.runner import run_worker
from sqlalchemy import select


def test_worker_once_processes_echo_job(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo", payload={"hello": "world"})
        job_id = job.id

    executed = run_worker(settings, once=True, worker_id="test-worker")
    assert executed == 1

    with session_scope(engine) as session:
        done = session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.result == {"echo": {"hello": "world"}}


def test_long_running_handler_refreshes_job_heartbeat(engine, settings, monkeypatch):
    calls: list[str] = []
    original = jobs.heartbeat

    def recording_heartbeat(session, job_id):
        calls.append(job_id)
        return original(session, job_id)

    monkeypatch.setattr(jobs, "heartbeat", recording_heartbeat)
    settings.worker_heartbeat_interval = 0.01
    settings.stale_job_seconds = 0.3
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.sleep", payload={"seconds": 0.25})
        job_id = job.id

    run_worker(settings, once=True, worker_id="heartbeat-test")
    assert job_id in calls


def test_worker_registers_and_deregisters(engine, settings):
    run_worker(settings, once=True, worker_id="test-worker-reg")
    with session_scope(engine) as session:
        registration = session.get(WorkerRegistration, "test-worker-reg")
        assert registration is not None
        assert registration.stopped_at is not None


def test_unknown_job_type_fails_without_retry(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "no.such.handler")
        job_id = job.id

    run_worker(settings, once=True, worker_id="test-worker")

    with session_scope(engine) as session:
        failed = session.get(Job, job_id)
        assert failed.status == JobStatus.FAILED
        assert "no handler" in failed.error


def test_fail_once_recovers_on_retry(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.fail_once", max_attempts=3)
        job_id = job.id

    # First pass: handler raises, job requeued with backoff.
    run_worker(settings, once=True, worker_id="test-worker")
    with session_scope(engine) as session:
        retried = session.get(Job, job_id)
        assert retried.status == JobStatus.QUEUED
        retried.run_at = utc_now()  # skip the backoff wait

    # Second pass: succeeds.
    run_worker(settings, once=True, worker_id="test-worker")
    with session_scope(engine) as session:
        done = session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.result["recovered"] is True
        assert done.result["attempt"] == 2


def test_worker_drains_multiple_jobs(engine, settings):
    with session_scope(engine) as session:
        for i in range(3):
            jobs.enqueue(session, "demo.echo", payload={"i": i})

    executed = run_worker(settings, once=True, worker_id="test-worker")
    assert executed == 3

    with session_scope(engine) as session:
        statuses = session.scalars(select(Job.status)).all()
        assert statuses == [JobStatus.SUCCEEDED] * 3


def test_permanent_source_job_error_is_not_retried(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(
            session,
            "sources.collect",
            payload={"source_id": "missing-source"},
            max_attempts=5,
        )
        job_id = job.id

    run_worker(settings, once=True, worker_id="test-permanent-error")
    with session_scope(engine) as session:
        failed = session.get(Job, job_id)
        assert failed.status == JobStatus.FAILED
        assert failed.attempts == 1
        assert "source not found" in failed.error
