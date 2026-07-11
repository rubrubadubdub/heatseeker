"""M0 acceptance: backup and restore proven end-to-end."""

import json

from heatseeker_common import backup, jobs
from heatseeker_common.db import create_db_engine, session_scope
from heatseeker_common.models import Job
from sqlalchemy import delete, select


def test_backup_and_restore_roundtrip(engine, settings):
    # Seed identifiable state: one job row + one raw-evidence file.
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo", payload={"marker": "precious"})
        job_id = job.id
    raw_file = settings.raw_dir / "ab" / "evidence.txt"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text("original evidence", encoding="utf-8")

    backup_dir = backup.create_backup(settings)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["raw_included"] is True
    assert manifest["alembic_version"] is not None

    # Destroy state: delete all jobs and the raw file.
    with session_scope(engine) as session:
        session.execute(delete(Job))
    raw_file.unlink()
    engine.dispose()  # release the DB file before replacing it (Windows requirement)

    backup.restore_backup(settings, backup_dir)

    restored_engine = create_db_engine(settings)
    try:
        with session_scope(restored_engine) as session:
            restored = session.get(Job, job_id)
            assert restored is not None
            assert restored.payload == {"marker": "precious"}
    finally:
        restored_engine.dispose()
    assert raw_file.read_text(encoding="utf-8") == "original evidence"


def test_restore_sets_current_db_aside(engine, settings):
    with session_scope(engine) as session:
        jobs.enqueue(session, "demo.echo")
    backup_dir = backup.create_backup(settings)
    engine.dispose()

    backup.restore_backup(settings, backup_dir)

    pre_restore = list(settings.resolved_data_dir.glob("heatseeker.db.pre-restore-*"))
    assert len(pre_restore) == 1  # old DB preserved, never deleted


def test_list_backups(engine, settings):
    with session_scope(engine) as session:
        jobs.enqueue(session, "demo.echo")
    backup.create_backup(settings)
    listed = backup.list_backups(settings)
    assert len(listed) == 1
    assert listed[0]["database_file"] == "heatseeker.db"


def test_backup_without_database_raises(settings, tmp_path):
    import pytest
    from heatseeker_common.settings import Settings

    empty = Settings(data_dir=tmp_path / "empty", _env_file=None)
    with pytest.raises(FileNotFoundError):
        backup.create_backup(empty)


def test_backup_survives_open_engine(engine, settings):
    """VACUUM INTO must work while the app engine holds connections (online backup)."""
    with session_scope(engine) as session:
        jobs.enqueue(session, "demo.echo")
    with engine.connect() as conn:  # hold a live connection during backup
        backup_dir = backup.create_backup(settings)
        assert (backup_dir / "heatseeker.db").exists()
        conn.close()

    # Snapshot contains the row.
    import sqlite3

    snap = sqlite3.connect(backup_dir / "heatseeker.db")
    try:
        count = snap.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    finally:
        snap.close()
    assert count == 1


def test_roundtrip_preserves_row_count(engine, settings):
    with session_scope(engine) as session:
        for i in range(5):
            jobs.enqueue(session, "demo.echo", payload={"i": i})
    backup_dir = backup.create_backup(settings)
    engine.dispose()
    backup.restore_backup(settings, backup_dir)

    restored_engine = create_db_engine(settings)
    try:
        with session_scope(restored_engine) as session:
            assert len(session.scalars(select(Job)).all()) == 5
    finally:
        restored_engine.dispose()
