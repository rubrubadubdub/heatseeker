"""Hardening coverage: stale-job reaping, pre-execution cancel, restore safety,
claim atomicity under concurrency, pack loader edge cases, settings validation."""

import threading
from datetime import timedelta

import pytest
from heatseeker_common import backup, jobs
from heatseeker_common.db import create_db_engine, session_scope
from heatseeker_common.models import Job, JobStatus, WorkerRegistration
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_industry_packs.loader import PackValidationError, load_pack
from heatseeker_worker.runner import WorkerRunner

# --- Stale-job reaper --------------------------------------------------------


def test_reaper_requeues_stale_running_job(engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo")
        job_id = job.id
    with session_scope(engine) as session:
        jobs.claim_next(session, "crashed-worker")
    # Simulate a dead worker: heartbeat far in the past.
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        job.heartbeat_at = utc_now() - timedelta(minutes=30)

    with session_scope(engine) as session:
        reaped = jobs.reap_stale_jobs(session, stale_after_seconds=120)
    assert reaped == 1
    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.QUEUED  # retry path, attempts still bounded
        assert "reaped" in job.error


def test_reaper_ignores_healthy_running_job(engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo")
        job_id = job.id
    with session_scope(engine) as session:
        jobs.claim_next(session, "healthy-worker")
    with session_scope(engine) as session:
        assert jobs.reap_stale_jobs(session, stale_after_seconds=120) == 0
        assert session.get(Job, job_id).status == JobStatus.RUNNING


# --- Cancel between claim and execution ---------------------------------------


def test_cancel_requested_before_execution_is_honoured(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo")
        job_id = job.id
    runner = WorkerRunner(settings, worker_id="test-worker")
    try:
        with session_scope(runner.engine) as session:
            claimed = jobs.claim_next(session, "test-worker")
            assert claimed.id == job_id
        with session_scope(runner.engine) as session:
            session.get(Job, job_id).cancel_requested = True

        runner._execute(job_id, "demo.echo", {}, 1)
    finally:
        runner.engine.dispose()

    with session_scope(engine) as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.result is None  # handler never ran


# --- Claim atomicity under concurrency ----------------------------------------


def test_concurrent_workers_never_double_claim(settings, engine):
    with session_scope(engine) as session:
        for i in range(20):
            jobs.enqueue(session, "demo.echo", payload={"i": i})

    claims: dict[str, list[str]] = {"w1": [], "w2": []}
    errors: list[Exception] = []

    def claim_all(worker_id: str) -> None:
        thread_engine = create_db_engine(settings)
        try:
            while True:
                with session_scope(thread_engine) as session:
                    job = jobs.claim_next(session, worker_id)
                    if job is None:
                        return
                    claims[worker_id].append(job.id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            thread_engine.dispose()

    threads = [threading.Thread(target=claim_all, args=(w,)) for w in claims]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    all_claims = claims["w1"] + claims["w2"]
    assert len(all_claims) == 20
    assert len(set(all_claims)) == 20  # no job claimed twice


# --- Restore safety ------------------------------------------------------------


def test_restore_refuses_when_worker_alive(engine, settings):
    with session_scope(engine) as session:
        jobs.enqueue(session, "demo.echo")
    backup_dir = backup.create_backup(settings)

    with session_scope(engine) as session:
        session.add(WorkerRegistration(id="alive-worker", hostname="h", pid=1))
    engine.dispose()

    with pytest.raises(RuntimeError, match="refusing to restore"):
        backup.restore_backup(settings, backup_dir)

    # force overrides after services are known-stopped
    backup.restore_backup(settings, backup_dir, force=True)


# --- Pack loader edge cases -----------------------------------------------------


def _minimal_pack(tmp_path, name="edge_pack"):
    pack_dir = tmp_path / name
    pack_dir.mkdir()
    (pack_dir / "manifest.yaml").write_text(
        f"schema: pack_manifest/v1\nid: {name}\nname: Edge\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    return pack_dir


def test_pack_rejects_oversized_file(tmp_path):
    pack_dir = _minimal_pack(tmp_path)
    big = "# filler\n" * 300_000  # > 2 MiB
    (pack_dir / "terminology.yaml").write_text("schema: terminology/v1\n" + big, encoding="utf-8")
    with pytest.raises(PackValidationError, match="exceeds"):
        load_pack(pack_dir)


def test_pack_rejects_non_utf8_file(tmp_path):
    pack_dir = _minimal_pack(tmp_path)
    (pack_dir / "terminology.yaml").write_bytes(b"\xff\xfe\x00bad bytes")
    # Rejected either as a parse error or as a non-mapping — never loaded.
    with pytest.raises(PackValidationError, match=r"terminology\.yaml"):
        load_pack(pack_dir)


def test_pack_rejects_empty_yaml_file(tmp_path):
    pack_dir = _minimal_pack(tmp_path)
    (pack_dir / "terminology.yaml").write_text("", encoding="utf-8")
    with pytest.raises(PackValidationError, match="expected a mapping"):
        load_pack(pack_dir)


# --- Settings validation ---------------------------------------------------------


def test_invalid_log_level_rejected(tmp_path):
    with pytest.raises(ValueError, match="log_level"):
        Settings(data_dir=tmp_path, log_level="VERBOSE", _env_file=None)


def test_log_level_normalised(tmp_path):
    assert Settings(data_dir=tmp_path, log_level="debug", _env_file=None).log_level == "DEBUG"
