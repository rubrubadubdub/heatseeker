# Architecture Overview

Derived from spec §7 and §29. Decisions with rationale live in
[decisions/](decisions/README.md); this doc describes the agreed shape.

## System layers (spec §7.2)

| Layer | Responsibility | Key modules |
|---|---|---|
| A. Industry configuration | Packs: taxonomies, rubrics, rules, vocabularies | `industry_packs` |
| B. Acquisition | Collectors: APIs, bulk files, feeds, HTML, PDF, manual input | `source_registry`, `collectors`, `crawler`, `parsers`, `documents` |
| C. Evidence | Immutable raw store + retrieval metadata | `evidence` (raw store), `SourceDocument` model |
| D. Intelligence processing | Parse → observe → resolve → classify → relate → score → cluster | `entity_resolution`, `classifications`, `knowledge_graph`, `confidence`, `market_intelligence` |
| E. Commercial action | Queues, leads, watchlists, alerts, exports, REST API | `lead_intelligence`, `research_orchestration`, `rules`, `integrations` |

## Canonical data flow

```
SourceIdentity aliases ──► SourceDefinition (canonical policy, authority, health)
                              ├─ SourceRelationship (ownership/copy lineage)
                              └─ SourceCoverage (correlated industry + region + facets)
                                   └─ collection job (coverage + scope snapshot,
                                      endpoint policy/budget enforced)
        └─ SourceDocument            immutable raw content + hash + retrieval metadata
             └─ parse (versioned parser)
                  └─ Observation     atomic statement, evidence-tied, extraction confidence
                       └─ entity resolution (match → merge queue, never destructive)
                            └─ FactAssertion   reconciled, scored (authority × freshness ×
                               │               corroboration × contradiction …)
                               ├─ ClassificationAssignment / CapabilityAssignment
                               ├─ Relationship / ProjectParticipation (graph edges)
                               └─ profiles, research questions, leads, queues, exports
```

Events flow in parallel: news/feeds → story clustering → Event → EventImpact →
macro-to-micro mapping → lead/watchlist/queue changes (spec §21–§22).

## Repository layout (planned; spec §29.1 baseline, ADR-0003)

```
heatseeker/
├── applications/
│   ├── api/            FastAPI app (localhost only) + server-rendered browser GUI
│   │                   (Jinja2 + Bootstrap + htmx, ADR-0009; templates/ + static/)
│   ├── worker/         job runner
│   └── cli/            operational commands (init, backup, import, crawl…)
├── packages/           Python packages, importable independently
│   ├── core_domain/    entities, schemas — industry-agnostic, no pack imports
│   ├── industry_packs/ pack loader, validation, versioning (+ packs/ data dirs)
│   ├── source_registry/ collectors/  crawler/  parsers/  documents/
│   ├── entity_resolution/  classifications/  knowledge_graph/  confidence/
│   ├── market_intelligence/  lead_intelligence/  research_orchestration/
│   ├── rules/  ai/  integrations/  common/
├── data/               runtime data — gitignored (raw/, processed/, exports/, backups/)
├── migrations/         Alembic
├── tests/              mirrors packages/; fixtures per spec §37.2
├── scripts/
├── configuration/      example configs, compose files
└── docs/
```

Dependency rule: `core_domain` imports nothing from other packages; packs are data +
loader, never imported by core; `applications/*` compose packages, packages never import
applications.

## Storage separation (spec §29.4)

| Store | Technology | Contents |
|---|---|---|
| Operational DB | SQLite (WAL) at `data/heatseeker.db` — ADR-0007 | entities, observations, facts, queues, jobs |
| Raw evidence | Filesystem under `data/raw/`, content-addressed (hash) | fetched bytes, documents; DB holds paths + hashes |
| Search index | SQLite FTS5 initially (M5+) | full-text; vector decided at M11 |
| Analytics | Parquet + DuckDB (later milestones) | snapshots, market aggregates |
| AI cache | DB table keyed by content-hash + prompt-version | model I/O audit + reuse |
| Backups | `data/backups/` | `VACUUM INTO` snapshot + raw-store copy; restore proven in M0 |

## Deployment (spec §29.3, §32.1)

Local development and local production modes only in Phase 1. API binds to
`127.0.0.1`; network exposure requires explicit configuration. Secrets via `.env`
(gitignored). Zero service dependencies (ADR-0007): `uv sync` then the `heatseeker` CLI
starts everything.

## Future-phase compatibility (spec §41.20)

The evidence chain (SourceDocument → Observation → FactAssertion), story clustering,
source-independence lineage, event/impact model, and outcome tracking are shared
concepts with the future financial-market system (see spec 2 in
[../spec/spec-index.md](../spec/spec-index.md)). Keep these industry-agnostic and avoid
scaffolding assumptions in their schemas.
