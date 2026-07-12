# Roadmap — Phase 1 Milestones

Distilled from spec §35 (lines 2968–3225). **This file is the single source of truth for
project status** — update the table when work starts or completes. Full deliverable
detail: read the spec section for the milestone via
[spec/spec-index.md](spec/spec-index.md); an implementation brief is created in
[milestones/](milestones/) when a milestone begins.

## Status

| # | Milestone | Status | Brief | Notes |
|---|---|---|---|---|
| M0 | Foundation | **Done** (2026-07-10; hardened 2026-07-11) | [M0-foundation.md](milestones/M0-foundation.md) | SQLite pivot: ADR-0007; reaper, safe cancel, restore guard |
| M1 | Industry-pack framework | **Done** (2026-07-11; hardened same day) | [M1-industry-packs.md](milestones/M1-industry-packs.md) | Rubrics/lead rules deferred to M5/M8 |
| UI | Browser GUI foundation (early, ADR-0009) | **Done** (2026-07-11, verified live) | — | Dashboard, jobs, packs, backups, health at `/`; API moved to `/api/*` |
| M2 | Source registry & evidence store | **Done** (2026-07-11, targeting hardened) | [M2-source-registry.md](milestones/M2-source-registry.md) | Contextual industry/region/facet coverage; ADR-0010 |
| — | Research scopes (tunable geo targeting: ANZ/APAC/custom states+cities) | **Done** (2026-07-11; regions-as-data + exclusions added same day, ADR-0012) | see M2 brief | M5+ must consume `scopes.active_scope()` |
| — | M2 robustness: grading, auto-deprecation, politeness, distillation (ADR-0011) | **Done** (2026-07-11) | see M2 brief | 98 tests green |
| M3 | Responsible crawler | **Done** (2026-07-11, acceptance verified) | [M3-crawler.md](milestones/M3-crawler.md) | Incl. transitive backlink discovery (vocabulary-gated proposals) |
| — | Document & evidence processing pipeline (PDF/OOXML/image extraction, HTML reference + publication-date extraction, manual evidence) | **Done** (2026-07-12) | see M2/M3 briefs | Migration 0011; versioned runs per (pipeline_version, config_hash); OCR/vision toggles exist but stay off until a provider lands (see gaps) |
| — | Agentic Source Scout (Codex/Claude plans, scope, budgets, schedules, proposals, optional policy-cleared auto-crawl) | **Done** (2026-07-12; ADR-0014) | — | Early M11 vertical slice; migration 0013 |
| M4 | Entity core & resolution | **Done** (2026-07-12; adversarially refined same day) | [M4-entities.md](milestones/M4-entities.md) | Multi-key blocking; priority dimensions; exact pointer/candidate reversal audit; ancestry + decision-bypass guards; nothing auto-merges |
| M5 | Company discovery & profile | Not started | — | |
| M6 | Projects, relationships, graph | Not started | — | |
| M7 | News, events, macro signals | Not started | — | |
| M8 | Lead intelligence | Not started | — | |
| M9 | Macro market workspace | Not started | — | |
| M10 | Programmatic action & integrations | Not started | — | |
| M11 | AI-assisted intelligence | Not started | — | |
| M12 | Feedback & calibration | Not started | — | |

Statuses: Not started · In progress · Blocked (say why in Notes) · Done (acceptance
verified).

## Milestone summaries (deliverables → acceptance)

**M0 Foundation** — repo scaffold, config, DB + migrations, raw storage, job framework,
logging, health page, backup, test framework. *Accepts:* starts locally; DB recreatable;
explicit data paths; observable jobs; backup/restore proven.

**M1 Industry-pack framework** — pack manifest/versioning, taxonomy + synonym import,
rubrics, lead rules, freshness policies, initial scaffolding pack. *Accepts:* second test
industry loads with **no core-table changes**; pack changes versioned; validation catches
bad config; scaffolding categories editable.

**M2 Source registry & evidence store** — SourceDefinition registry, raw-document model,
collector interfaces (API/feed/file/HTML), access-policy metadata, hashing, source
health. Includes pack seed-list sync + source lifecycle (proposed→candidate→active) per
[architecture/source-discovery.md](architecture/source-discovery.md), plus correlated
industry/geography/facet coverage and source-lineage relationships (ADR-0010). *Accepts:* original
evidence preserved; duplicate retrieval recognised; source failure isolated; policy
visible.

**M3 Responsible crawler** — robots (RFC 9309), frontier with purpose/lineage, domain
budgets, rate limiting, conditional retrieval, sitemaps, generic HTML extraction, parser
profiles, change detection. *Accepts:* disallowed paths never crawled; per-domain limits
hold; lineage retained; changes create new observations without deleting history.

**M4 Entity core & resolution** — Organisation/OperationalUnit/Location/identifier/
contact models, merge+split, match scoring, resolution queue. *Accepts:* duplicates
found and safely merged; branch/parent not flattened; ambiguous matches reviewable;
**every merge reversible**.

**M5 Company discovery & profile** — discovery workflow, profile UI, evidence viewer,
classification, capabilities, geography, size/tier estimates, research gaps. *Accepts:*
regional population discovered and profiled; every field shows evidence + confidence;
missing stays missing; conflicts visible.

**M6 Projects, relationships, graph** — projects, participation, relationships, graph
query layer, project workspace. *Accepts:* companies connect via projects/relationships;
edge confidence inspectable; history keeps dates; multi-hop queries work.

**M7 News, events, macro signals** — news collection, story clustering, event taxonomy,
market signals, event-impact mapping, watchlists. *Accepts:* duplicate reporting
clusters; one event affects many segments/orgs; fact vs inferred impact separated;
events shift research/lead priority.

**M8 Lead intelligence** — offering definitions, lead scoring, timing signals, negative
indicators, contactability, lead queue, account brief, suppression. *Accepts:* leads per
offering; scores explained; weak evidence lowers confidence; suppression respected; **no
automatic outreach**.

**M9 Macro market workspace** — market definitions, segment/geography analysis, trend
indices, project pipeline, tender/award aggregation, methodology display. *Accepts:*
defensible market report; every estimate states coverage + uncertainty; macro→micro maps;
snapshots preserved.

**M10 Programmatic action & integrations** — rule engine, REST API, CSV/JSON export,
change feed, adapter framework, optional Odoo boundary. *Accepts:* intelligence creates
tasks/queues; stable external identifiers; exports respect suppression; integration
failure can't corrupt core.

**M11 AI-assisted intelligence** — AI-in-the-loop is the default posture and seams may be
exercised from M2 onward (ADR-0008: Anthropic API + Claude Code/Codex CLI providers) —
provider abstraction (per ADR-0006), structured
extraction, classification/contradiction/research-gap assistants, account briefing,
AI source-expansion proposals (Reddit/forums/directories — see
[architecture/source-discovery.md](architecture/source-discovery.md)), local+remote
modes, AI audit. *Accepts:* AI disableable; results cite evidence;
unsupported claims rejected/flagged; model swaps need no schema change; AI failure
doesn't block deterministic flows.

**M12 Feedback & calibration** — user-confirmed facts, lead outcomes, classification
corrections, score/source/research performance reporting. *Accepts:* system learns
source/signal usefulness; decisions auditable; scoring versions comparable; corrections
improve workflows.

## Known gaps (audited 2026-07-11 — tracked, not forgotten)

| Gap | Why it matters | Lands |
|---|---|---|
| Structured API adapters (ABR/ASIC/NZBN keys, params, pagination) | Official registries are the backbone of company facts; plain GET only reaches their homepages | M4/M5 (entity ingestion) |
| Bulk dataset import (CSV/XLSX/Parquet, spec §12.2) | MVP requires ≥1 official dataset import | M4/M5 |
| RSS/Atom per-entry parsing (feeds stored as one XML doc today) | News/story clustering needs per-article documents | M7 (or earlier) |
| Autopilot-scheduled crawls (crawls are on-demand) | Needs a cadence policy to avoid 21 sites × every tick | short follow-up |
| OCR/vision extraction (`evidence_ocr_enabled`/`evidence_vision_enabled` exist, default off, no provider wired) | Scanned PDFs and images carry text the native extractors can't reach; runs record OCR as disabled/unavailable rather than pretending | OCR: any time via a free local engine (e.g. Tesseract/RapidOCR — running-cost-free rule: free deps fine, paid services not). Semantic vision: M11 |
| JS rendering (Playwright, permitted-only) | JS-only sites yield little today | when a real source needs it |
| AI pass-2 source vetting + terms interpretation | Funnel pass 2 | M11 |

## Sequencing notes

- M0–M4 are strictly ordered (each depends on the previous).
- M5 needs M1+M4; M6–M8 build on M5; M9 needs M7; M10 can start after M5; M11 slots onto
  seams reserved from M0 (ADR-0006); M12 needs M8+M11.
- Build vertical slices early (spec §40): from M2 onward keep one thin end-to-end path
  (source → evidence → observation → entity → profile) working rather than perfecting
  layers in isolation.
- MVP (spec §36) ≈ M0–M8 plus exports from M10; check the MVP list before deferring
  anything.
