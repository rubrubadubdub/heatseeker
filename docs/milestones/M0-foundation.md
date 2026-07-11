# M0 — Foundation

**Status:** Done — acceptance verified live 2026-07-10 (mirror in [../roadmap.md](../roadmap.md))
**Spec:** §35 M0 (lines 2970–2992); context §29 (architecture), §30 (jobs), §38 (observability)
**Depends on:** ADR-0002…0004 (promote to Accepted, or supersede, as part of this milestone)

## Goal

A running skeleton: app starts locally with one command, Postgres up via Docker Compose,
migrations apply, jobs run observably, raw-evidence paths exist, backup/restore proven,
tests green in CI-less local run. No domain features — just durable rails.

## Deliverables

1. **Repo scaffold** per ADR-0004: uv workspace, `applications/{api,worker,cli}`,
   `packages/{core_domain,common}` (others created when their milestone starts),
   `migrations/`, `tests/`, `configuration/`, root `pyproject.toml`, Ruff config.
2. **Configuration**: `.env`-driven settings (Pydantic Settings) with explicit,
   printable data paths (`data/raw`, `data/exports`, `data/backups`, logs); example env
   file committed, real `.env` gitignored.
3. **Database**: SQLite in WAL mode at `data/heatseeker.db` (ADR-0007); Alembic wired;
   initial migration creating only infrastructure tables (job, audit_log,
   worker_registration, app_meta).
4. **Job framework** (spec §30): Postgres-backed job table + worker loop —
   enqueue/claim/heartbeat/retry-with-backoff/cancel; priority classes; per-job audit
   rows. Keep it minimal (~few hundred lines) per ADR-0002.
5. **Logging**: structured (JSON) logs to stderr + rotating file under data path;
   job/HTTP correlation ids.
6. **Health page**: `GET /health` reporting DB connectivity, migration head, worker
   liveness, data-path writability, disk headroom.
7. **Backup/restore**: CLI commands `backup create` (`VACUUM INTO` snapshot + raw-store
   copy → `data/backups/<timestamp>/`) and `backup restore <path>`; both exercised by a
   test.
8. **Test framework**: pytest + fixtures for a throwaway test DB (schema per test run),
   settings override, and a demo job test.
9. **CLI entrypoint** (`heatseeker …`): `init`, `serve`, `worker`, `backup`,
   `migrate`, `health`.

## Acceptance (spec §35 M0)

- [x] `heatseeker migrate && heatseeker serve` starts the app locally; `/health` is
      green (HTTP 200, all checks ok — verified 2026-07-10).
- [x] Database can be dropped and recreated from migrations alone (every test run does
      this in a tmp dir; live instance migrated from empty).
- [x] Data paths are explicit — shown by `heatseeker health`/`init`, all under `data/`.
- [x] Jobs can be observed: demo.echo enqueued via CLI, executed by `worker --once`,
      result visible via `jobs show` and `GET /jobs`.
- [x] Backup and restore proven end-to-end by automated tests (tests/test_backup.py)
      and live (`backup create` → `backup restore --yes` → history intact).

## Boundaries / cautions

- No domain tables yet — resist modelling Organisation here; that's M4 (schema informed
  by M1–M3 needs).
- API binds 127.0.0.1 only (spec §32.1).
- Windows is the host: keep paths `pathlib`-clean. Docker was verified absent at M0
  start → ADR-0007 pivoted the operational store to SQLite.
- Update ADR statuses and the roadmap table when done.
