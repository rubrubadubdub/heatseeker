"""Heatseeker operational CLI (M0): init, migrate, serve, worker, health, backup, jobs."""

import json
import re
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from heatseeker_common import backup as backup_module
from heatseeker_common import jobs as jobs_module
from heatseeker_common.db import create_db_engine, session_scope
from heatseeker_common.health import check_health
from heatseeker_common.logging import configure_logging
from heatseeker_common.migrate import upgrade_to_head
from heatseeker_common.models import Job
from heatseeker_common.settings import Settings
from sqlalchemy import select

app = typer.Typer(no_args_is_help=True, help="Heatseeker — niche industry intelligence platform")
backup_app = typer.Typer(no_args_is_help=True, help="Backup and restore")
jobs_app = typer.Typer(no_args_is_help=True, help="Job queue operations")
packs_app = typer.Typer(no_args_is_help=True, help="Industry pack operations")
sources_app = typer.Typer(no_args_is_help=True, help="Canonical sources and coverage pairings")
scopes_app = typer.Typer(no_args_is_help=True, help="Research-scope operations")
app.add_typer(backup_app, name="backup")
app.add_typer(jobs_app, name="jobs")
app.add_typer(packs_app, name="packs")
app.add_typer(sources_app, name="sources")
app.add_typer(scopes_app, name="scopes")


def _settings() -> Settings:
    return Settings()


def _echo_json(data) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command()
def init() -> None:
    """Create data directories and report resolved paths."""
    settings = _settings()
    paths = settings.ensure_data_dirs()
    typer.echo("Data paths:")
    for name, path in paths.items():
        typer.echo(f"  {name:<8} {path}")
    typer.echo(f"  {'db':<8} {settings.resolved_database_url}")
    typer.echo("Next: heatseeker migrate")


@app.command()
def migrate() -> None:
    """Apply database migrations to head."""
    settings = _settings()
    upgrade_to_head(settings)
    typer.echo("Migrations applied (head).")


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Override bind host (default 127.0.0.1)"),
    port: int | None = typer.Option(None, help="Override port (default 8100)"),
) -> None:
    """Run the local API server + browser GUI."""
    settings = _settings()
    configure_logging(settings, "api")
    from heatseeker_api.main import create_app

    bind_host = host or settings.api_host
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            f"WARNING: binding to {bind_host} exposes the app beyond this machine "
            "(spec section 32.1 expects localhost-only by default).",
            fg=typer.colors.YELLOW,
            err=True,
        )
    bind_port = port or settings.api_port
    typer.echo(f"Heatseeker UI: http://{bind_host}:{bind_port}/")
    uvicorn.run(
        create_app(settings),
        host=bind_host,
        port=bind_port,
        log_config=None,  # keep our JSON logging
    )


@app.command()
def run(
    host: str | None = typer.Option(None, help="Override bind host (default 127.0.0.1)"),
    port: int | None = typer.Option(None, help="Override port (default 8100)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the browser"),
) -> None:
    """One-command launch: migrate, start worker + API/GUI, open the browser.

    This is what Heatseeker.bat runs. Ctrl+C (or closing the window) stops everything.
    """
    import threading
    import webbrowser

    settings = _settings()
    # Quiet, human-readable console; full JSON detail goes to data/logs/app.log.
    configure_logging(settings, "app", console_json=False, console_level="WARNING")
    typer.echo("Starting Heatseeker...")
    upgrade_to_head(settings)

    from heatseeker_api.main import create_app
    from heatseeker_worker.runner import run_worker

    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=run_worker,
        kwargs={"settings": settings, "stop_event": stop_event},
        name="heatseeker-worker",
        daemon=True,
    )
    worker_thread.start()

    bind_host = host or settings.api_host
    bind_port = port or settings.api_port
    url = f"http://{bind_host}:{bind_port}/"
    typer.echo()
    typer.secho(f"  Heatseeker is running:  {url}", fg=typer.colors.GREEN, bold=True)
    typer.echo("  If no browser appeared, it may have opened as a tab in an existing")
    typer.echo(f"  browser window - or open the link yourself: {url}")
    typer.echo()
    typer.echo("  This window is the engine - keep it open while you use the app.")
    typer.echo("  Close it (or press Ctrl+C) to stop Heatseeker.")
    typer.echo(f"  Detailed logs: {settings.logs_dir / 'app.log'}")
    typer.echo()

    def _open_browser() -> None:
        try:
            opened = webbrowser.open(url, new=1)
        except Exception:
            opened = False
        if not opened:
            typer.secho(
                f"  Could not launch a browser automatically - open {url} manually.",
                fg=typer.colors.YELLOW,
            )

    if not no_browser:
        threading.Timer(1.5, _open_browser).start()

    try:
        uvicorn.run(
            create_app(settings),
            host=bind_host,
            port=bind_port,
            log_config=None,
        )
    finally:
        stop_event.set()
        worker_thread.join(timeout=10)
        typer.echo("Heatseeker stopped.")


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Drain eligible jobs, then exit"),
) -> None:
    """Run the job worker."""
    settings = _settings()
    configure_logging(settings, "worker")
    from heatseeker_worker.runner import run_worker

    executed = run_worker(settings, once=once)
    if once:
        typer.echo(f"Executed {executed} job(s).")


@app.command()
def health() -> None:
    """Print a health report; exit 1 when degraded."""
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        report = check_health(engine, settings)
    finally:
        engine.dispose()
    _echo_json(report)
    if report["status"] != "ok":
        raise typer.Exit(code=1)


@backup_app.command("create")
def backup_create() -> None:
    """Snapshot the database (VACUUM INTO) and raw store into data/backups/<timestamp>/."""
    settings = _settings()
    dest = backup_module.create_backup(settings)
    typer.echo(f"Backup created: {dest}")


@backup_app.command("list")
def backup_list() -> None:
    _echo_json(backup_module.list_backups(_settings()))


@backup_app.command("restore")
def backup_restore(
    path: Annotated[Path, typer.Argument(help="Backup directory (data/backups/<timestamp>)")],
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
    force: bool = typer.Option(False, "--force", help="Restore even if a worker looks alive"),
) -> None:
    """Restore from a backup. Stop the API and worker first."""
    settings = _settings()
    if not yes:
        typer.confirm(
            f"Replace live database at {settings.database_path} from {path}? "
            "(API/worker must be stopped; current DB is set aside, not deleted)",
            abort=True,
        )
    try:
        target = backup_module.restore_backup(settings, path, force=force)
    except (RuntimeError, FileNotFoundError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Restored database to {target}")


@jobs_app.command("enqueue")
def jobs_enqueue(
    job_type: str = typer.Argument(..., help="e.g. demo.echo"),
    payload: str = typer.Option("{}", help="JSON payload"),
    priority: int = typer.Option(
        50, help="Priority class value (10=interactive .. 70=maintenance)"
    ),
    max_attempts: int = typer.Option(3),
) -> None:
    try:
        payload_data = json.loads(payload)
        if not isinstance(payload_data, dict):
            raise ValueError("payload must be a JSON object, e.g. '{\"key\": 1}'")
    except (json.JSONDecodeError, ValueError) as exc:
        typer.secho(f"Invalid --payload: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if not 1 <= priority <= 100:
        typer.secho(
            "Invalid --priority: expected 1-100 (10=interactive .. 70=maintenance)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            job = jobs_module.enqueue(
                session,
                job_type,
                payload=payload_data,
                priority=priority,
                max_attempts=max_attempts,
                actor="cli",
            )
            typer.echo(f"Enqueued {job.job_type} id={job.id}")
    finally:
        engine.dispose()


@jobs_app.command("list")
def jobs_list(
    status: str | None = typer.Option(None),
    limit: int = typer.Option(20),
) -> None:
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Job.status == status)
        with session_scope(engine) as session:
            rows = [
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "priority": job.priority,
                    "attempts": job.attempts,
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                }
                for job in session.scalars(stmt)
            ]
        _echo_json(rows)
    finally:
        engine.dispose()


@jobs_app.command("show")
def jobs_show(job_id: str) -> None:
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                typer.echo("job not found", err=True)
                raise typer.Exit(code=1)
            _echo_json(
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "priority": job.priority,
                    "payload": job.payload,
                    "result": job.result,
                    "error": job.error,
                    "attempts": job.attempts,
                    "max_attempts": job.max_attempts,
                    "run_at": job.run_at,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "claimed_by": job.claimed_by,
                    "cancel_requested": job.cancel_requested,
                }
            )
    finally:
        engine.dispose()


@packs_app.command("list")
def packs_list() -> None:
    """Discover packs on disk and show validation + registration status."""
    from heatseeker_industry_packs.loader import PackValidationError, discover_packs, load_pack
    from heatseeker_industry_packs.models import PackRegistration

    settings = _settings()
    engine = create_db_engine(settings)
    rows = []
    try:
        with session_scope(engine) as session:
            for pack_path in discover_packs():
                try:
                    pack = load_pack(pack_path)
                    registered = session.get(PackRegistration, pack.pack_id)
                    rows.append(
                        {
                            "pack_id": pack.pack_id,
                            "name": pack.manifest.name,
                            "version": pack.version,
                            "valid": True,
                            "registered_version": registered.version if registered else None,
                            "path": str(pack_path),
                        }
                    )
                except PackValidationError as exc:
                    rows.append(
                        {
                            "pack_id": pack_path.name,
                            "valid": False,
                            "problems": exc.problems,
                            "path": str(pack_path),
                        }
                    )
    finally:
        engine.dispose()
    _echo_json(rows)


@packs_app.command("validate")
def packs_validate(
    path: Annotated[Path, typer.Argument(help="Pack directory to validate")],
) -> None:
    """Validate a pack directory; exit 1 with all problems listed if invalid."""
    from heatseeker_industry_packs.loader import PackValidationError, load_pack

    try:
        pack = load_pack(path)
    except PackValidationError as exc:
        _echo_json({"valid": False, "problems": exc.problems})
        raise typer.Exit(code=1) from None
    _echo_json(
        {
            "valid": True,
            "pack_id": pack.pack_id,
            "version": pack.version,
            "content_hash": pack.content_hash,
            "files": sorted(pack.files),
        }
    )


@packs_app.command("load")
def packs_load(
    pack_id: Annotated[str, typer.Argument(help="Pack id (directory name under packs root)")],
) -> None:
    """Validate a pack and register it (version + content hash) in the database."""
    from heatseeker_industry_packs.loader import (
        PackValidationError,
        default_packs_root,
        load_pack,
    )
    from heatseeker_industry_packs.registry import register_pack

    settings = _settings()
    try:
        pack = load_pack(default_packs_root() / pack_id)
    except PackValidationError as exc:
        _echo_json({"valid": False, "problems": exc.problems})
        raise typer.Exit(code=1) from None

    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            registration = register_pack(session, pack, actor="cli")
            typer.echo(
                f"Loaded pack {registration.pack_id} v{registration.version} "
                f"(hash {registration.content_hash[:12]}...)"
            )
    finally:
        engine.dispose()


@jobs_app.command("cancel")
def jobs_cancel(job_id: str) -> None:
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            ok = jobs_module.cancel(session, job_id, actor="cli")
        typer.echo("cancelled" if ok else "not cancellable (missing or already finished)")
        if not ok:
            raise typer.Exit(code=1)
    finally:
        engine.dispose()


@sources_app.command("list")
def sources_list(
    status: str | None = typer.Option(None, help="Lifecycle status"),
    industry: str | None = typer.Option(None, help="Industry target"),
    region: str | None = typer.Option(None, help="Region target"),
    include_unknown: bool = typer.Option(False, help="Keep unknown target dimensions"),
) -> None:
    """List canonical sources, optionally resolving one industry-region context."""
    from heatseeker_source_registry.models import SourceCoverage, SourceDefinition
    from heatseeker_source_registry.targeting import match_coverages
    from sqlalchemy.orm import selectinload

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        stmt = (
            select(SourceDefinition)
            .options(selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets))
            .order_by(SourceDefinition.authority_tier, SourceDefinition.name)
        )
        if status:
            stmt = stmt.where(SourceDefinition.lifecycle_status == status)
        with session_scope(engine) as session:
            rows = []
            for source in session.scalars(stmt):
                match = match_coverages(
                    source.coverages,
                    industry_ids=[industry] if industry else (),
                    region_codes=[region] if region else (),
                    include_unknown=include_unknown,
                )
                if (industry or region) and not match:
                    continue
                rows.append(
                    {
                        "id": source.id,
                        "name": source.name,
                        "category": source.source_category,
                        "lifecycle_status": source.lifecycle_status,
                        "authority_tier": source.authority_tier,
                        "coverage_count": len(source.coverages),
                        "matched_coverage_keys": list(match.matched_coverage_keys),
                    }
                )
        _echo_json(rows)
    finally:
        engine.dispose()


@sources_app.command("show")
def sources_show(source_id: str) -> None:
    """Show a source with identities and full coverage profiles."""
    from heatseeker_source_registry.models import SourceCoverage, SourceDefinition
    from heatseeker_source_registry.targeting import serialize_coverage
    from sqlalchemy.orm import selectinload

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        stmt = (
            select(SourceDefinition)
            .where(SourceDefinition.id == source_id)
            .options(
                selectinload(SourceDefinition.coverages).selectinload(SourceCoverage.targets),
                selectinload(SourceDefinition.identities),
            )
        )
        with session_scope(engine) as session:
            source = session.scalars(stmt).first()
            if source is None:
                typer.echo("source not found", err=True)
                raise typer.Exit(code=1)
            result = {
                "id": source.id,
                "name": source.name,
                "category": source.source_category,
                "base_url": source.base_url,
                "lifecycle_status": source.lifecycle_status,
                "identities": [
                    {
                        "type": identity.identity_type,
                        "value": identity.identity_value,
                        "primary": identity.is_primary,
                    }
                    for identity in source.identities
                ],
                "coverages": [
                    {"id": coverage.id, **serialize_coverage(coverage)}
                    for coverage in source.coverages
                ],
            }
        _echo_json(result)
    finally:
        engine.dispose()


@sources_app.command("add")
def sources_add(
    name: str = typer.Argument(...),
    category: str = typer.Option(...),
    url: str | None = typer.Option(None),
    access_method: str = typer.Option("html"),
    authority_tier: int = typer.Option(5, min=1, max=7),
) -> None:
    """Register a canonical candidate source; pairing is a separate action."""
    from heatseeker_common import audit
    from heatseeker_source_registry.identity import (
        attach_identity,
        resolve_identities,
        url_identity,
    )
    from heatseeker_source_registry.models import SourceDefinition

    name = name.strip()
    category = category.strip().lower().replace(" ", "_")
    if (
        not name
        or len(name) > 300
        or len(category) > 50
        or not re.fullmatch(r"[a-z][a-z0-9_]*", category)
    ):
        typer.secho("name/category is invalid", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    allowed_access = {"api", "bulk", "rss", "sitemap", "html", "rendered", "manual"}
    if access_method not in allowed_access:
        typer.secho(f"invalid access method: {access_method}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if access_method != "manual" and not url:
        typer.secho("--url is required for automated sources", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if url and len(url) > 1000:
        typer.secho("--url exceeds 1000 characters", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    try:
        identity = url_identity(url) if url else None
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            if identity and resolve_identities(session, [identity]) is not None:
                typer.secho("a source with this URL already exists", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            source = SourceDefinition(
                name=name,
                source_category=category,
                base_url=url,
                access_method=access_method,
                authority_tier=authority_tier,
                origin="user",
            )
            session.add(source)
            session.flush()
            if identity:
                attach_identity(session, source, identity, origin="user", is_primary=True)
            audit.record(session, "cli", "source.created", "source", source.id)
            result = {"id": source.id, "name": source.name}
        _echo_json(result)
    finally:
        engine.dispose()


@sources_app.command("pair")
def sources_pair(
    source_id: str,
    coverage_key: str = typer.Option(..., help="Stable key within this source"),
    industries: str = typer.Option("", help="Comma-separated industry ids"),
    regions: str = typer.Option("", help="Comma-separated geography codes"),
    priority: int = typer.Option(50, min=0, max=100),
) -> None:
    """Add or update one coherent industry-region coverage pairing."""
    from heatseeker_source_registry.models import SourceDefinition
    from heatseeker_source_registry.targeting import (
        CoverageSpec,
        TargetSpec,
        serialize_coverage,
        upsert_coverage,
    )

    targets = [TargetSpec("industry", value) for value in industries.split(",") if value.strip()]
    targets.extend(
        TargetSpec("region", value, match_mode="hierarchical")
        for value in regions.split(",")
        if value.strip()
    )
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            source = session.get(SourceDefinition, source_id)
            if source is None:
                typer.echo("source not found", err=True)
                raise typer.Exit(code=1)
            try:
                coverage, outcome = upsert_coverage(
                    session,
                    source,
                    CoverageSpec(
                        coverage_key=coverage_key,
                        targets=tuple(targets),
                        priority=priority,
                        origin="user",
                    ),
                    actor="cli",
                )
            except ValueError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from None
            result = {"id": coverage.id, "outcome": outcome, **serialize_coverage(coverage)}
        _echo_json(result)
    finally:
        engine.dispose()


@sources_app.command("sync")
def sources_sync(
    pack_id: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview and roll back"),
) -> None:
    """Reconcile stable identities and pack-provenanced coverage profiles."""
    from heatseeker_industry_packs.loader import (
        PackValidationError,
        default_packs_root,
        load_pack,
    )
    from heatseeker_source_registry.sync import sync_pack_seeds

    try:
        pack = load_pack(default_packs_root() / pack_id)
    except PackValidationError as exc:
        _echo_json({"valid": False, "problems": exc.problems})
        raise typer.Exit(code=1) from None
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            result = sync_pack_seeds(session, pack, actor="cli")
            if dry_run:
                session.rollback()
        _echo_json({"dry_run": dry_run, **result})
    finally:
        engine.dispose()


@sources_app.command("collect")
def sources_collect(
    source_id: str,
    coverage_id: str | None = typer.Option(None, help="Explicit coherent coverage id"),
    scope_id: str | None = typer.Option(None, help="Research scope (defaults to active)"),
) -> None:
    """Queue collection with immutable coverage and research-scope context."""
    from heatseeker_source_registry.models import (
        ResearchScope,
        SourceCoverage,
        SourceDefinition,
    )
    from heatseeker_source_registry.policy import activation_blockers
    from heatseeker_source_registry.scopes import active_scope
    from heatseeker_source_registry.targeting import match_coverages

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            source = session.get(SourceDefinition, source_id)
            if source is None:
                typer.echo("source not found", err=True)
                raise typer.Exit(code=1)
            if source.lifecycle_status not in {"active", "degraded"}:
                typer.secho(
                    f"source is {source.lifecycle_status}, not collectable",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
            coverage = session.get(SourceCoverage, coverage_id) if coverage_id else None
            if coverage_id and (coverage is None or coverage.source_definition_id != source_id):
                typer.echo("source coverage not found", err=True)
                raise typer.Exit(code=1)
            blockers = activation_blockers(source, coverage)
            if blockers:
                typer.secho("; ".join(blockers), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=1)
            scope = session.get(ResearchScope, scope_id) if scope_id else active_scope(session)
            if scope_id and scope is None:
                typer.echo("scope not found", err=True)
                raise typer.Exit(code=1)
            if (
                coverage
                and scope
                and not match_coverages(
                    [coverage],
                    industry_ids=scope.industry_ids,
                    region_codes=scope.geo_codes,
                    target_filters=scope.target_filters,
                    include_unknown=scope.include_unknown,
                )
            ):
                typer.echo("coverage does not match the selected scope", err=True)
                raise typer.Exit(code=1)
            scope_snapshot = (
                {
                    "id": scope.id,
                    "name": scope.name,
                    "geo_codes": scope.geo_codes,
                    "industry_ids": scope.industry_ids,
                    "target_filters": scope.target_filters,
                    "include_unknown": scope.include_unknown,
                }
                if scope
                else None
            )
            job = jobs_module.enqueue(
                session,
                "sources.collect",
                payload={
                    "schema_version": 2,
                    "source_id": source_id,
                    "coverage_id": coverage_id,
                    "pairing_ids": [coverage_id] if coverage_id else [],
                    "scope_id": scope.id if scope else None,
                    "scope_snapshot": scope_snapshot,
                },
                priority=10,
                actor="cli",
            )
            result = {"job_id": job.id, "payload": job.payload}
        _echo_json(result)
    finally:
        engine.dispose()


@sources_app.command("check-policy")
def sources_check_policy(
    source_id: str,
    coverage_id: str | None = typer.Option(None, help="Coverage-specific endpoint"),
) -> None:
    """Queue a source- or coverage-endpoint robots policy check."""
    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            job = jobs_module.enqueue(
                session,
                "sources.check_policy",
                payload={"source_id": source_id, "coverage_id": coverage_id},
                priority=10,
                actor="cli",
            )
            result = {"job_id": job.id, "payload": job.payload}
        _echo_json(result)
    finally:
        engine.dispose()


@scopes_app.command("list")
def scopes_list() -> None:
    from heatseeker_source_registry.models import ResearchScope

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            rows = [
                {
                    "id": scope.id,
                    "name": scope.name,
                    "geo_codes": scope.geo_codes,
                    "industry_ids": scope.industry_ids,
                    "target_filters": scope.target_filters,
                    "include_unknown": scope.include_unknown,
                    "is_active": scope.is_active,
                }
                for scope in session.scalars(select(ResearchScope).order_by(ResearchScope.name))
            ]
        _echo_json(rows)
    finally:
        engine.dispose()


@scopes_app.command("activate")
def scopes_activate(scope_id: str) -> None:
    from heatseeker_source_registry.scopes import set_active

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            scope = set_active(session, scope_id, actor="cli")
            if scope is None:
                typer.echo("scope not found", err=True)
                raise typer.Exit(code=1)
            result = {"id": scope.id, "name": scope.name, "is_active": scope.is_active}
        _echo_json(result)
    finally:
        engine.dispose()


@scopes_app.command("create")
def scopes_create(
    name: str,
    regions: str = typer.Option("", help="Comma-separated geography codes"),
    industries: str = typer.Option("", help="Comma-separated industry ids"),
    include_unknown: bool = typer.Option(True, help="Retain genuinely unknown coverage"),
) -> None:
    """Create a reusable multi-dimensional research scope."""
    from heatseeker_source_registry.models import ResearchScope
    from heatseeker_source_registry.scopes import create_scope

    settings = _settings()
    engine = create_db_engine(settings)
    try:
        with session_scope(engine) as session:
            if session.scalars(
                select(ResearchScope).where(ResearchScope.name == name.strip())
            ).first():
                typer.echo("scope name already exists", err=True)
                raise typer.Exit(code=1)
            try:
                scope = create_scope(
                    session,
                    name,
                    regions,
                    actor="cli",
                    industry_ids_raw=industries,
                    include_unknown=include_unknown,
                )
            except ValueError as exc:
                typer.secho(str(exc), fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2) from None
            result = {"id": scope.id, "name": scope.name}
        _echo_json(result)
    finally:
        engine.dispose()


if __name__ == "__main__":
    app()
