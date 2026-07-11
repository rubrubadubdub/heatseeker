# Heatseeker — Niche Industry Intelligence Platform (Phase 1)

Local-first intelligence platform that discovers, verifies, and organises information
about hard-to-research industries, then converts it into evidence-backed market insight
and lead intelligence. First industry pack: scaffolding / temporary works (AU/NZ focus).
Long-term: reusable engine for opaque industries; later phases add financial-market
intelligence.

## Current state

**M0, M1, and M2 are built, hardened, and verified**, with a **browser GUI** (ADR-0009:
Jinja2 + Bootstrap + htmx at `/`, JSON API under `/api/*`). uv workspace, SQLite+Alembic,
job queue+worker, CLI, backup/restore, pack framework + scaffolding pack, **source
registry with robots/terms policy gate, content-addressed evidence store, and tunable
multi-dimensional research scopes**, contextual source coverage (industry + geography +
extensible facets, ADR-0010), canonical identities, and source-lineage relationships.
M5+ features must respect `scopes.active_scope()`. **M3 responsible crawler is done**:
budgeted frontier with lineage (`crawler.py`), per-URL robots, sitemaps, and transitive
backlink discovery (vocabulary-gated proposals into the vetting funnel). Plus source
grading/auto-deprecation, autopilot, distillation token pipes (ADR-0011). 110 tests
green. M4 (entity core & resolution) next — turns evidence into companies.
Check [docs/roadmap.md](docs/roadmap.md) for milestone status before starting any work.
Dev loop: `uv sync` · `uv run pytest -q` · `uv run ruff check .` · `uv run heatseeker run`
User launch path: `Heatseeker.bat` (repo root) / desktop shortcut → `heatseeker run`
(migrate + in-process worker + GUI + browser). Keep it working.
UI pattern: add a template under `applications/api/src/heatseeker_api/templates/` + a
route in `ui_routes.py`; UI calls Python functions directly, never its own HTTP API.

## Reading rules (token discipline)

1. **Never read the full specs in `docs/` top level.** They are ~50k tokens combined.
   - Start with [docs/spec/spec-digest.md](docs/spec/spec-digest.md) (condensed Phase 1 requirements).
   - Need detail? Use [docs/spec/spec-index.md](docs/spec/spec-index.md) to find the exact
     section, then `Read` the master spec with `offset`/`limit` for just that section.
2. **Spec files are frozen inputs — never edit them** (line numbers in spec-index depend on it).
3. Working on a milestone? Read its brief in `docs/milestones/` first; it contains
   everything needed for that milestone.
4. Route by task using [docs/README.md](docs/README.md).

## Documentation map

| Doc | Purpose |
|---|---|
| [docs/README.md](docs/README.md) | Doc index + per-task reading routes |
| [docs/spec/spec-digest.md](docs/spec/spec-digest.md) | Condensed Phase 1 spec (read this, not the master) |
| [docs/spec/spec-index.md](docs/spec/spec-index.md) | Section→line-number map of both master specs |
| [docs/roadmap.md](docs/roadmap.md) | Milestones M0–M12, acceptance criteria, **status tracking** |
| [docs/architecture/overview.md](docs/architecture/overview.md) | Layers, module layout, data flow |
| [docs/architecture/data-model.md](docs/architecture/data-model.md) | Mandatory conceptual entities & distinctions |
| [docs/architecture/decisions/](docs/architecture/decisions/README.md) | ADRs (stack, storage, pack format, AI boundary) |
| [docs/architecture/source-discovery.md](docs/architecture/source-discovery.md) | Expandable source list + AI source expansion (Reddit etc.) |
| [docs/conventions.md](docs/conventions.md) | Code, test, git, and doc-maintenance conventions |
| [docs/glossary.md](docs/glossary.md) | Domain vocabulary (observation vs fact, claimed vs evidenced, …) |
| [docs/milestones/](docs/milestones/) | Per-milestone implementation briefs |

## Non-negotiable product rules (full list in spec-digest)

- **Evidence before assertion**: source document → observation → fact assertion are
  distinct records; never collapse them.
- **Missing ≠ false**: absence of evidence must not become evidence of absence. Abstain
  rather than fabricate.
- **Provenance everywhere**: every fact carries source, observed date, confidence, freshness.
- **Responsible collection only**: robots.txt is always inspected and recorded; its
  enforcement follows ADR-0013's global/per-source policy. Never bypass auth, paywalls,
  or CAPTCHAs; always retain per-domain politeness, budgets, and clear crawler identity.
- **Industry-agnostic core**: scaffolding knowledge lives in the industry pack, never in
  core tables. A second test pack must load without architectural rework.
- **AI-in-the-loop by default, bounded** (ADR-0008): replaceable providers (Anthropic
  API; Claude Code / Codex CLI adapters for agentic research), schema-constrained output
  with evidence citations; robots/budget enforcement stays deterministic code; core
  still runs with AI disabled (§41.19).
- **No destructive merges**: entity merges preserve originals and are reversible.
- **No automatic outreach** in Phase 1.

## Tech stack (baseline — see ADRs before deviating)

Python 3.12+ (managed by uv) · FastAPI · Pydantic v2 · SQLAlchemy 2 + Alembic · SQLite
(WAL, ADR-0007 — no Docker on target machine) · filesystem raw-evidence store · pytest ·
Ruff · Typer CLI · GUI: server-rendered Jinja2 + Bootstrap 5 + htmx, vendored, no Node
(ADR-0009). Details and rationale:
[docs/architecture/decisions/](docs/architecture/decisions/README.md).

## Conventions (highlights — full list in docs/conventions.md)

- Domain terms use the spec's British/Australian spellings in code and schema
  (`Organisation`, `normalisation`), because the spec's YAML schemas do.
- After completing work: update the milestone status in `docs/roadmap.md`; record any
  deviation from the spec or an ADR as a new ADR.
- **Git is mandatory and atomic** (docs/conventions.md#git): one logical change per
  commit, tests+lint green first, schema+migration+tests together, push to origin
  after each completed unit; tag milestones `m<N>-complete`. Never leave work
  uncommitted at end of session.
