"""Backup and restore (spec §29.3, §29.5).

Backup: consistent online snapshot via SQLite `VACUUM INTO` + copies of raw evidence and
versioned processed artifacts. Restore: replaces the live DB file — the API and worker
must be stopped first;
the current DB is set aside (never deleted) before the backup copy lands.
"""

import json
import shutil
import sqlite3
from pathlib import Path

from heatseeker_common import __version__
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now

MANIFEST_NAME = "manifest.json"
DB_NAME = "heatseeker.db"


def create_backup(settings: Settings) -> Path:
    settings.ensure_data_dirs()
    if not settings.database_path.exists():
        raise FileNotFoundError(f"no database at {settings.database_path} — nothing to back up")

    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    dest = settings.backups_dir / stamp
    dest.mkdir(parents=True, exist_ok=False)

    # VACUUM INTO produces a consistent snapshot even with WAL readers/writers active.
    source = sqlite3.connect(settings.database_path)
    try:
        source.execute("VACUUM INTO ?", (str(dest / DB_NAME),))
    finally:
        source.close()

    raw_included = False
    if settings.raw_dir.exists() and any(settings.raw_dir.iterdir()):
        shutil.copytree(settings.raw_dir, dest / "raw")
        raw_included = True

    processed_included = False
    if settings.processed_dir.exists() and any(settings.processed_dir.iterdir()):
        shutil.copytree(settings.processed_dir, dest / "processed")
        processed_included = True

    snapshot = sqlite3.connect(dest / DB_NAME)
    try:
        row = snapshot.execute("SELECT version_num FROM alembic_version").fetchone()
        alembic_version = row[0] if row else None
    except sqlite3.OperationalError:
        alembic_version = None
    finally:
        snapshot.close()

    manifest = {
        "created_at": utc_now().isoformat(),
        "database_file": DB_NAME,
        "raw_included": raw_included,
        "processed_included": processed_included,
        "alembic_version": alembic_version,
        "app_version": __version__,
    }
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def list_backups(settings: Settings) -> list[dict]:
    results = []
    if not settings.backups_dir.exists():
        return results
    for entry in sorted(settings.backups_dir.iterdir()):
        manifest_path = entry / MANIFEST_NAME
        if entry.is_dir() and manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            results.append({"path": str(entry), **manifest})
    return results


def _live_worker_heartbeat(db_path: Path, within_seconds: float) -> str | None:
    """Return a worker id with a recent heartbeat in the given DB, else None."""
    from datetime import timedelta

    cutoff = (utc_now() - timedelta(seconds=within_seconds)).replace(tzinfo=None)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM worker_registration"
            " WHERE stopped_at IS NULL AND heartbeat_at > ? LIMIT 1",
            (cutoff.isoformat(sep=" "),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table absent (pre-migration DB) — nothing to protect
    finally:
        conn.close()
    return row[0] if row else None


def restore_backup(settings: Settings, backup_path: Path, force: bool = False) -> Path:
    """Replace live DB (and merge raw store) from a backup directory.

    Caller must ensure API/worker are stopped and all engines disposed. The existing DB
    is moved aside as heatseeker.db.pre-restore-<ts>, never deleted. Refuses to run when
    a worker heartbeat is recent in the live DB unless force=True.
    """
    backup_path = Path(backup_path)
    backup_db = backup_path / DB_NAME
    if not (backup_path / MANIFEST_NAME).exists() or not backup_db.exists():
        raise FileNotFoundError(f"not a valid backup directory: {backup_path}")

    settings.ensure_data_dirs()
    target = settings.database_path

    if target.exists() and not force:
        live = _live_worker_heartbeat(target, within_seconds=60.0)
        if live:
            raise RuntimeError(
                f"refusing to restore: worker '{live}' has a recent heartbeat in the live "
                "database. Stop the worker (and API), or pass force=True / --force."
            )

    # Set the current state aside; drop stale WAL/SHM sidecars for the replaced file.
    if target.exists():
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        target.rename(target.with_name(f"{target.name}.pre-restore-{stamp}"))
    for suffix in ("-wal", "-shm"):
        sidecar = target.with_name(target.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    shutil.copy2(backup_db, target)

    backup_raw = backup_path / "raw"
    if backup_raw.exists():
        shutil.copytree(backup_raw, settings.raw_dir, dirs_exist_ok=True)
    backup_processed = backup_path / "processed"
    if backup_processed.exists():
        shutil.copytree(backup_processed, settings.processed_dir, dirs_exist_ok=True)
    return target
