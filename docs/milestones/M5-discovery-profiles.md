# M5 — Company Discovery & Profile

Spec: §9.2 (discovery), §12.2 (bulk datasets), §13.6–§13.7 + §13.14–§13.16 (schemas),
§15 (classification), §16 (size/tier), §17 (confidence), §18 (profile UI), §35 M5.

## Deliverables (spec §35 M5)

Discovery workflow · company profile · evidence viewer · classification · service
capabilities · geography · size/tier estimates · research gaps.

**Acceptance:** initial regional company population discovered and profiled · every
profile field exposes evidence + confidence · missing stays missing · conflicts visible.

## Design decisions

1. **Package**: one new `packages/intelligence` → `heatseeker_intelligence` housing the
   whole evidence→conclusion layer (observations, facts, confidence, classification,
   capabilities, sizing, gaps, discovery, profile). The overview's planned layout
   sketched separate `classifications/`/`confidence/` packages; consolidating avoids
   package sprawl while keeping module boundaries — recorded here as the deviation.
2. **Evidence chain lands here** (§6.1, mandatory distinction #1): `observation` (what a
   source said, tied to a SourceDocument + row/page location) and `fact_assertion` (what
   we conclude) are separate tables. One assertion per (entity, predicate), updated in
   place with `rule_version`; history lives in observations, which are never overwritten.
   Profile reads compare assertion evidence sets with the current M4 merge group and
   reconcile only when needed, so merge/reversal cannot expose stale absorbed facts.
3. **Confidence is deterministic, field-level, and inspectable** (§17.2): multiplicative
   composition of authority × extraction × match × freshness × corroboration ×
   contradiction, every component stored on the assertion. Manual entry is explicitly
   separate from human verification (`human_verified`, verifier, timestamp). Freshness half-lives vary by
   predicate (§17.4, table in `confidence.py`). Corroboration counts **distinct
   sources** only (same-source repeats don't corroborate; full lineage-independence
   deferred to M7 story clustering, noted). Numeric + §17.7 vocabulary both exposed.
4. **Missing ≠ false**: no observation ⇒ no assertion ⇒ profile shows "unknown" and a
   research question — never a fabricated value. Size estimates emit only bands with
   evidence behind them; otherwise `unresolved` (§16.1, §39.3).
5. **Conflicts are first-class** (§17.6): contradicting observations attach to the
   assertion, force `conflicted` status when contested, stay visible in the profile,
   and auto-generate research questions.
6. **Classification is multi-axis and explainable** (§15): pack taxonomies
   (archetypes/services/segments/project types) + the spec's fixed business-model axis.
   Every assignment carries assignment_type (registered/self-described/observed/
   inferred/human-confirmed/rejected), evidence ids, classifier_version. M5 ships
   deterministic rules only; AI rubric classification is an M11 seam.
7. **Capability status ladder is code** (§13.7): claimed (self-description only) →
   evidenced (independent source) → repeatedly-evidenced (≥3 evidence refs, ≥2 sources)
   → verified (human) · historical/uncertain/contradicted by evidence age and conflict.
8. **Discovery = bulk dataset import + dedupe funnel** (§9.2, §12.2, MVP "≥1 official
   dataset import"): bounded CSV/one-CSV-in-ZIP upload with a column mapping, full import
   provenance (`bulk_import_run`: publisher, version, coverage date, licence note,
   checksum, mapping, rejected rows), the raw file preserved as a SourceDocument under
   a dedicated bulk-imports SourceDefinition. Rows become observations → find-or-create
   organisations (identifier match first, then exact name+locality+country only when all
   are present) → reconciled facts →
   `entities.match_scan` queued so duplicates land in the M4 resolution queue.
   **Scope-aware and reproducible**: the active scope is snapshotted when queued and rows
   outside that immutable snapshot are skipped and counted. User datasets default to
   conservative authority tier 5; higher authority is an explicit, audited declaration.
   Optional pack-mapped service/archetype columns populate evidence-linked
   classifications and capability ladders.
9. **Profile is the M4 entity page grown into a workspace** (§18): identity + duplicate
   warnings (M4), commercial summary (classifications, capabilities, size bands, tier),
   evidence summary per fact (value, confidence vocab + components, freshness, source
   count, contradiction count, best-evidence link into the existing evidence viewer),
   research gaps with resolve/dismiss. Identity, classification, capability, and sizing
   sections expose confidence and evidence-document links. Manual observations require an
   existing evidence document and are not verified unless the user explicitly confirms.

## Schema (migrations 0015 + hardening 0016)

`observation` · `fact_assertion` (unique entity+predicate; component scores; supporting/
contradicting observation id lists) · `classification_assignment` (pack_id + taxonomy_id
+ category_id as pack-scoped strings — taxonomies are pack data, not core tables) ·
`capability_assignment` · `size_estimate` (one per organisation × concept: legal entity /
operating group / local branch / capability tier / commercial sophistication /
procurement sophistication / outsourcing need) · `research_question` ·
`bulk_import_run` (including scope + authority snapshots). Migration 0016 also adds
explicit verification metadata to `observation`.

## Module map (packages/intelligence)

| Module | Contents |
|---|---|
| `models.py` | tables + enums above |
| `confidence.py` | component composition, freshness policies, §17.7 vocabulary |
| `observations.py` | record/query observations with document provenance |
| `facts.py` | `reconcile()` per entity+predicate: group values, score, status ladder |
| `classifications.py` | axis registry (fixed + pack), assign/retract, deterministic rules |
| `capabilities.py` | ladder transitions from evidence shape |
| `sizing.py` | indicator gathering → band/tier per concept, `unresolved` default |
| `gaps.py` | research-question generation from missing/conflicted/stale |
| `discovery.py` | CSV import: provenance, mapping, scope filter, entity funnel |
| `profile.py` | profile assembly consumed by UI + JSON API |
| worker `handlers/discovery.py` | `discovery.import_csv` job |
| API/UI | `/discovery` page; profile sections on `/entities/{id}`; `/api/companies/{id}/profile`, `/api/discovery/*`, research-question actions |

## Acceptance → test map

| Acceptance | Test |
|---|---|
| regional population discovered + profiled | `test_discovery_import.py`: CSV/ZIP → organisations, identifiers, observations, facts, pack classifications/capabilities, immutable scope filter, dedupe scan queued |
| every field exposes evidence + confidence | `test_intelligence_profile.py` + `test_m5_api.py`: fact components and identity/commercial/size observation → document links |
| missing stays missing | no-observation predicates absent/unknown; size `unresolved`; gap generated instead |
| conflicts visible | contradicting observations preserved, `conflicted` status, contradiction count in profile, research question spawned |

## Hardening audit (2026-07-12)

Adversarial tests cover precomputed facts across merge/reversal, ambiguous same-name
companies with incomplete geography, import failure persistence, queued-scope drift,
conservative/declared authority, full-name region normalisation, pack claim ingestion,
manual-entry versus verification, evidence links, ZIP ambiguity, and populated 0015 ↔
0016 migration round trips. Reconciliation also proves that independently repeated weak
directories cannot outvote a primary authority, while preserving the disagreement.

## Qualification hardening (2026-07-18)

- Structured discovery accepts bounded CSV, JSON, and JSONL exports (including one
  supported file in a ZIP), removing a spreadsheet-conversion choke point while keeping
  raw-document and row/line provenance.
- Import matching uses an import-local identifier and exact name/place index instead of
  scanning the whole organisation population per row. Ambiguous exact keys remain
  deliberately unmatched for M4 review.
- Immutable scope snapshots now enforce geography, industry, and include/exclude target
  facets. Dataset `coverage_date` drives observation freshness instead of import time.
- Bulk service/archetype claims are low-authority observations, never registered facts;
  tier-5 and weak-source capability signals remain hypotheses until independently
  corroborated. Invalid mapped values survive as rejected observations for inspection.
- Corroboration collapses source-copy/ownership lineage, cached facts and capabilities
  age without new evidence, and system-generated gap questions close when the underlying
  evidence gap is filled.
- Public business profiles from Instagram, Facebook, LinkedIn organisations, YouTube,
  TikTok, X, Threads, Pinterest, and Reddit communities import as normalised,
  evidence-linked, multi-valued contact identities. The generic social column supports
  mixed URLs, while platform columns also accept handles.
- Exact shared public-profile URLs strengthen the same M4 resolution queue used by
  registers, domains, phones, addresses, and names. They are never deterministic merge
  keys: a profile by itself remains human review, preserving the ability to assemble
  evidence across many sources without conflating similarly named businesses.
- Permitted crawls can propose exact linked public profiles with discovery lineage, but
  those proposals are manual-only weak signals and are never fetched automatically.
  No stored platform-login page is required; future official API tokens remain external.
