# ADR-0003: PostgreSQL operational store + filesystem raw-evidence store

**Status:** Superseded by [ADR-0007](0007-sqlite-operational-store.md) (Docker absent on
target machine — the revisit clause below fired at M0 start) · **Date:** 2026-07-10

## Context

Spec §29.4 requires separated storage: operational data, immutable raw evidence, search
index, analytics, AI cache, backups. Concurrent writers exist from M2 onward (API +
worker + crawler). Spec suggests PostgreSQL with full-text and optional vector; DuckDB/
Parquet for analytics. Deployment is local, on the user's Windows machine.

## Decision

- **PostgreSQL 16 via Docker Compose** as the operational store (entities, observations,
  fact assertions, queues, jobs, audit, AI cache).
- **Postgres FTS** for search initially; pgvector only if/when semantic search is
  actually built (M11+), not before.
- **Raw evidence on the filesystem** under `data/raw/`, content-addressed by SHA-256
  (`data/raw/ab/cd/abcd….bin` + sidecar metadata row in Postgres). Raw bytes never go in
  the DB; the DB stores path, hash, and retrieval metadata (spec §13.13).
- **DuckDB + Parquet** for analytical snapshots — deferred until M9 needs them.
- **Backups**: `pg_dump` + raw-store copy via a CLI command; restore path proven as part
  of M0 acceptance.

## Consequences

One extra service (Postgres container) but real concurrency, FTS, JSONB for the spec's
many `JSON` fields, and a mature migration path. Evidence store stays cheap, immutable,
and rsync-able.

## Alternatives rejected

- **SQLite (+FTS5)**: simplest install, but concurrent crawler/worker/API writers,
  JSONB-heavy schemas, and FTS needs make Postgres the safer baseline. Revisit only if
  Docker proves to be a deployment blocker on the target machine.
- **Raw evidence as DB blobs**: bloats backups, complicates inspection and dedupe.
- **Dedicated graph DB**: spec §23.3 explicitly says relational graph patterns first.
