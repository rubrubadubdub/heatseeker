# ADR-0002: Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy 2 backend

**Status:** Accepted (validated by M0 implementation, 2026-07-10) · **Date:** 2026-07-10

## Context

The system is collection/processing/analytics-heavy: crawling, parsing, entity
resolution, scoring, document extraction, AI adapters. Spec §29.2 suggests a Python
baseline and instructs avoiding unnecessary infrastructure. Single-user local
deployment; no horizontal-scale pressure.

## Decision

- **Python 3.12+** for all backend code (applications and packages).
- **FastAPI** for the local REST API; **Pydantic v2** for schemas/validation (also the
  contract for AI structured output later).
- **SQLAlchemy 2.x** ORM + **Alembic** migrations.
- **uv** for dependency/environment management (fast, lockfile, workspace support for
  the monorepo package layout).
- **pytest** for tests; **Ruff** for lint + format (line length 100).
- Job runner: start with a thin Postgres-backed job table + worker loop we own
  (spec §30 requires observable, auditable, budgeted jobs — most off-the-shelf queues
  hide exactly the state we must expose). Re-evaluate (e.g. Procrastinate) at M0 if the
  custom loop grows beyond ~a few hundred lines.

## Consequences

One language across collection, processing, API, and CLI; the richest scraping/parsing
ecosystem (httpx, selectolax/BeautifulSoup, Playwright, pdfplumber, Protego). Frontend
(React, later milestones) is a separate ADR when M5 nears.

## Alternatives rejected

- **Node/TypeScript end-to-end**: weaker document-extraction and data tooling.
- **Celery/RQ/Redis**: adds a broker service for a single-user local app; Postgres
  already provides durable queueing at this scale.
