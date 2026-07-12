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
3. **Confidence is deterministic, field-level, and inspectable** (§17.2): multiplicative
   composition of authority × extraction × match × freshness × corroboration ×
   contradiction, every component stored on the assertion. Freshness half-lives vary by
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
   dataset import"): CSV/CSV-in-ZIP upload with a column mapping, full import
   provenance (`bulk_import_run`: publisher, version, coverage date, licence note,
   checksum, mapping, rejected rows), the raw file preserved as a SourceDocument under
   a dedicated bulk-imports SourceDefinition. Rows become observations → find-or-create
   organisations (identifier match first, then exact name+locality) → reconciled facts →
   `entities.match_scan` queued so duplicates land in the M4 resolution queue.
   **Scope-aware**: rows outside `scopes.active_scope()` geography are skipped and
   counted, honouring the M5+ scope rule.
9. **Profile is the M4 entity page grown into a workspace** (§18): identity + duplicate
   warnings (M4), commercial summary (classifications, capabilities, size bands, tier),
   evidence summary per fact (value, confidence vocab + components, freshness, source
   count, contradiction count, best-evidence link into the existing evidence viewer),
   research gaps with resolve/dismiss. Contacts already ship with M4.

## Schema (migration 0014)

`observation` · `fact_assertion` (unique entity+predicate; component scores; supporting/
contradicting observation id lists) · `classification_assignment` (pack_id + taxonomy_id
+ category_id as pack-scoped strings — taxonomies are pack data, not core tables) ·
`capability_assignment` · `size_estimate` (one per organisation × concept: legal entity /
operating group / local branch / capability tier / commercial sophistication /
procurement sophistication / outsourcing need) · `research_question` ·
`bulk_import_run`.

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
| regional population discovered + profiled | `test_discovery_import.py`: CSV → organisations, identifiers, observations, facts, provenance chain, scope filter, dedupe scan queued |
| every field exposes evidence + confidence | `test_profile.py`: each fact carries confidence components, vocab, observation → document links |
| missing stays missing | no-observation predicates absent/unknown; size `unresolved`; gap generated instead |
| conflicts visible | contradicting observations preserved, `conflicted` status, contradiction count in profile, research question spawned |
