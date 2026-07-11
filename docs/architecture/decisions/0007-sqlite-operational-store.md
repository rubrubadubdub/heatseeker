# ADR-0007: SQLite as the operational store (supersedes ADR-0003)

**Status:** Accepted · **Date:** 2026-07-10 · **Supersedes:** ADR-0003

## Context

ADR-0003 proposed PostgreSQL via Docker Compose with an explicit revisit clause: "only
if Docker proves to be a deployment blocker on the target machine." Verified at M0
start: Docker Desktop is not installed on the target Windows 11 machine (uninstalled;
only log remnants under `%ProgramData%\DockerDesktop`). Installing it requires admin
rights and a user decision, and would contradict the spec's one-command local install
goal (§29.5) for a single-user deployment.

## Decision

- **SQLite** (stdlib driver via SQLAlchemy `sqlite+pysqlite`) as the operational store,
  file at `data/heatseeker.db`. WAL journal mode, `busy_timeout`, `foreign_keys=ON`,
  `synchronous=NORMAL` set per-connection.
- **Concurrency model**: WAL gives many readers + one writer — sufficient for one API
  process + one worker at single-user scale. Job claiming uses an atomic
  `UPDATE … RETURNING` (SQLite ≥3.35) instead of `FOR UPDATE SKIP LOCKED`.
- **Portability discipline**: schema stays on generic SQLAlchemy types (`JSON`, not
  JSONB; string UUIDs; timezone-aware UTC datetimes), migrations use
  `render_as_batch=True`, and no SQLite-only SQL in domain code — preserving the
  migration path back to Postgres if a server deployment ever materialises (spec §29.3
  "small private server" future mode).
- **Search**: SQLite FTS5 when search features arrive (M5+); semantic/vector search
  decided at M11.
- **Backup**: `VACUUM INTO` for a consistent online snapshot + raw-store copy —
  simpler and safer than pg_dump orchestration.
- Raw-evidence store, DuckDB/Parquet analytics, and AI-cache decisions from ADR-0003
  carry over unchanged.

## Consequences

Zero service dependencies: `uv sync` + one CLI command runs everything. Backup/restore
becomes file-level and trivially testable. The ceiling (write concurrency, no server
access) is acceptable for Phase 1's single-user scope; a later Postgres return is a
migration task, not a redesign.

## Alternatives rejected

- **Install Docker Desktop**: admin + licensing + user decision; heavy dependency for
  one database serving one user.
- **Embedded/portable Postgres binaries**: brittle on Windows, unmaintained wrappers.
