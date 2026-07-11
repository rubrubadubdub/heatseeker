"""Health checks (spec §29.5: clear health status; §38 observability)."""

import shutil
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from heatseeker_common.migrate import find_migrations_dir, script_head
from heatseeker_common.models import WorkerRegistration
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now

DISK_WARN_BYTES = 1 * 1024 * 1024 * 1024  # warn below 1 GiB free
WORKER_STALE_FACTOR = 3  # heartbeats older than 3x interval count as stale


def check_health(engine: Engine, settings: Settings) -> dict:
    checks: dict[str, dict] = {}

    # Database connectivity
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"status": "ok", "url": settings.resolved_database_url}
    except Exception as exc:
        checks["database"] = {"status": "fail", "error": str(exc)}

    # Migrations at head
    try:
        head = script_head(find_migrations_dir())
        with engine.connect() as conn:
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        status = "ok" if current == head else "fail"
        checks["migrations"] = {"status": status, "current": current, "head": head}
    except Exception as exc:
        checks["migrations"] = {"status": "fail", "error": str(exc)}

    # Worker liveness (absence is informational, not a failure — worker may not be running)
    try:
        stale_cutoff = utc_now() - timedelta(
            seconds=settings.worker_heartbeat_interval * WORKER_STALE_FACTOR
        )
        with engine.connect() as conn:
            # Select the column itself (not max()) so UTCDateTime re-attaches tzinfo.
            latest = conn.execute(
                select(WorkerRegistration.heartbeat_at)
                .where(WorkerRegistration.stopped_at.is_(None))
                .order_by(WorkerRegistration.heartbeat_at.desc())
                .limit(1)
            ).scalar()
        if latest is None:
            checks["worker"] = {"status": "absent"}
        elif latest >= stale_cutoff:
            checks["worker"] = {"status": "ok", "last_heartbeat": latest.isoformat()}
        else:
            checks["worker"] = {"status": "stale", "last_heartbeat": latest.isoformat()}
    except Exception as exc:
        checks["worker"] = {"status": "fail", "error": str(exc)}

    # Data paths exist and are writable
    paths_detail: dict[str, str] = {}
    paths_ok = True
    for name, path in settings.data_paths().items():
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            paths_detail[name] = str(path)
        except Exception as exc:
            paths_detail[name] = f"NOT WRITABLE ({exc})"
            paths_ok = False
    checks["data_paths"] = {"status": "ok" if paths_ok else "fail", "paths": paths_detail}

    # Disk headroom
    try:
        usage = shutil.disk_usage(settings.resolved_data_dir)
        checks["disk"] = {
            "status": "ok" if usage.free > DISK_WARN_BYTES else "warn",
            "free_bytes": usage.free,
        }
    except Exception as exc:
        checks["disk"] = {"status": "fail", "error": str(exc)}

    critical = ("database", "migrations", "data_paths")
    degraded = any(checks[name]["status"] == "fail" for name in critical)
    return {"status": "degraded" if degraded else "ok", "checks": checks}
