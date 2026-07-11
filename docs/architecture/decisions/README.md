# Architecture Decision Records

Short records of significant decisions. The spec (§40) grants the development agent
judgement but requires meaningful deviations to be documented — this is where.

## Process

- One decision per file: `NNNN-short-title.md`.
- Statuses: **Proposed** (baseline adopted from spec, revisit-able at implementation
  time) → **Accepted** (validated in working code) → **Superseded by ADR-NNNN**.
- A Proposed ADR is the default path: follow it unless you have a concrete reason not
  to; if you deviate, write a superseding ADR *before* implementing.
- Keep them short: Context / Decision / Consequences / Alternatives rejected.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-python-fastapi-backend.md) | Python 3.12 + FastAPI + Pydantic + SQLAlchemy backend | Accepted |
| [0003](0003-postgres-and-filesystem-evidence-store.md) | PostgreSQL operational store + filesystem raw-evidence store | Superseded by 0007 |
| [0004](0004-monorepo-layout.md) | Monorepo with applications/ + packages/ layout | Accepted |
| [0005](0005-industry-packs-as-versioned-yaml.md) | Industry packs as versioned YAML directories | Proposed |
| [0006](0006-ai-provider-abstraction.md) | AI behind a provider-agnostic, optional adapter | Accepted (amended by 0008) |
| [0007](0007-sqlite-operational-store.md) | SQLite operational store (WAL) + filesystem raw-evidence store | Accepted |
| [0008](0008-ai-in-the-loop-default.md) | AI-in-the-loop by default; agentic CLI providers (Claude Code/Codex) | Accepted |
| [0009](0009-server-rendered-gui.md) | Server-rendered GUI: FastAPI + Jinja2 + Bootstrap 5 + htmx (vendored) | Accepted |
| [0011](0011-collection-robustness.md) | Grading, auto-deprecation, politeness-not-evasion, storage/token economy | Accepted |
| [0010](0010-contextual-source-coverage.md) | Contextual source coverage profiles | Accepted |
