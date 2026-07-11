"""Worker loop: register, claim, execute, heartbeat, retry (spec §30)."""

import logging
import os
import socket
import threading
import time
import traceback

from heatseeker_common import jobs
from heatseeker_common.db import create_db_engine, session_scope
from heatseeker_common.job_registry import JobContext, PermanentJobError, get_handler
from heatseeker_common.models import WorkerRegistration
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from sqlalchemy.engine import Engine

from heatseeker_worker import handlers  # noqa: F401 — registers all job handlers

logger = logging.getLogger("heatseeker.worker")


class WorkerRunner:
    def __init__(self, settings: Settings, worker_id: str | None = None):
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.engine: Engine = create_db_engine(settings)

    def register(self) -> None:
        with session_scope(self.engine) as session:
            existing = session.get(WorkerRegistration, self.worker_id)
            if existing:
                existing.heartbeat_at = utc_now()
                existing.stopped_at = None
            else:
                session.add(
                    WorkerRegistration(
                        id=self.worker_id, hostname=socket.gethostname(), pid=os.getpid()
                    )
                )

    def heartbeat(self) -> None:
        with session_scope(self.engine) as session:
            registration = session.get(WorkerRegistration, self.worker_id)
            if registration:
                registration.heartbeat_at = utc_now()

    def deregister(self) -> None:
        with session_scope(self.engine) as session:
            registration = session.get(WorkerRegistration, self.worker_id)
            if registration:
                registration.stopped_at = utc_now()

    def maybe_enqueue_autopilot(self) -> bool:
        """Enqueue a sources.autopilot job unless one is already pending/running.

        Enqueued (not run inline) so every tick is observable on the Jobs page.
        """
        if not self.settings.autopilot_enabled:
            return False
        from heatseeker_common.models import Job, JobStatus, PriorityClass
        from sqlalchemy import select

        with session_scope(self.engine) as session:
            pending = session.scalars(
                select(Job.id)
                .where(
                    Job.job_type == "sources.autopilot",
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .limit(1)
            ).first()
            if pending:
                return False
            jobs.enqueue(
                session,
                "sources.autopilot",
                priority=PriorityClass.SCHEDULED_PRIORITY,
                actor="worker-autopilot",
            )
        return True

    def reap_stale(self) -> None:
        """Recover jobs orphaned by crashed/killed workers (heartbeat gone silent)."""
        with session_scope(self.engine) as session:
            reaped = jobs.reap_stale_jobs(session, self.settings.stale_job_seconds)
        if reaped:
            logger.warning("reaped stale jobs", extra={"count": reaped})

    def run(
        self,
        once: bool = False,
        max_jobs: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> int:
        """Process jobs until stopped. once=True: drain eligible jobs, then return.
        stop_event allows an embedding process (heatseeker run) to stop the loop cleanly.

        Returns the number of jobs executed.
        """
        self.register()
        self.reap_stale()
        executed = 0
        last_heartbeat = time.monotonic()
        last_autopilot = 0.0  # fire on first pass so a fresh install starts collecting
        logger.info("worker started", extra={"worker_id": self.worker_id})
        try:
            while not (stop_event and stop_event.is_set()):
                if time.monotonic() - last_heartbeat >= self.settings.worker_heartbeat_interval:
                    self.heartbeat()
                    self.reap_stale()
                    last_heartbeat = time.monotonic()
                if not once and (
                    time.monotonic() - last_autopilot >= self.settings.autopilot_interval_seconds
                ):
                    if self.maybe_enqueue_autopilot():
                        logger.info("autopilot tick enqueued")
                    last_autopilot = time.monotonic()

                with session_scope(self.engine) as session:
                    job = jobs.claim_next(session, self.worker_id)
                    claimed = (
                        None
                        if job is None
                        else (job.id, job.job_type, dict(job.payload), job.attempts)
                    )
                # Claim is committed here; execution happens outside that transaction.

                if claimed is None:
                    if once:
                        return executed
                    if stop_event:
                        stop_event.wait(self.settings.worker_poll_interval)
                    else:
                        time.sleep(self.settings.worker_poll_interval)
                    continue

                self._execute(*claimed)
                executed += 1
                if max_jobs is not None and executed >= max_jobs:
                    return executed
            return executed
        except KeyboardInterrupt:
            logger.info("worker interrupted", extra={"worker_id": self.worker_id})
            return executed
        finally:
            self.deregister()
            self.engine.dispose()

    def _execute(self, job_id: str, job_type: str, payload: dict, attempt: int) -> None:
        # Cancellation may have been requested between claim-commit and execution.
        with session_scope(self.engine) as session:
            job = session.get(jobs.Job, job_id)
            if job is not None and job.cancel_requested:
                jobs.mark_cancelled(session, job_id, "cancel requested before execution")
                logger.info("job cancelled pre-execution", extra={"job_id": job_id})
                return

        handler = get_handler(job_type)
        if handler is None:
            with session_scope(self.engine) as session:
                jobs.mark_failed(
                    session, job_id, f"no handler registered for '{job_type}'", retryable=False
                )
            logger.error("no handler", extra={"job_id": job_id, "job_type": job_type})
            return

        ctx = JobContext(
            job_id=job_id,
            job_type=job_type,
            payload=payload,
            attempt=attempt,
            engine=self.engine,
            logger=logger,
        )
        try:
            result = handler(ctx)
        except PermanentJobError:
            error = traceback.format_exc()
            with session_scope(self.engine) as session:
                jobs.mark_failed(session, job_id, error, retryable=False)
            logger.warning(
                "job failed permanently",
                extra={"job_id": job_id, "job_type": job_type, "attempt": attempt},
            )
        except Exception:
            error = traceback.format_exc()
            with session_scope(self.engine) as session:
                jobs.mark_failed(session, job_id, error)
            logger.warning(
                "job failed", extra={"job_id": job_id, "job_type": job_type, "attempt": attempt}
            )
        else:
            with session_scope(self.engine) as session:
                jobs.mark_succeeded(session, job_id, result)
            logger.info(
                "job succeeded", extra={"job_id": job_id, "job_type": job_type, "attempt": attempt}
            )


def run_worker(
    settings: Settings,
    once: bool = False,
    max_jobs: int | None = None,
    worker_id: str | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    return WorkerRunner(settings, worker_id=worker_id).run(
        once=once, max_jobs=max_jobs, stop_event=stop_event
    )
