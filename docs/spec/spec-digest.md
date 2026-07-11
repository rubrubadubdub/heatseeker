# Phase 1 Spec Digest

Condensed from the master spec (`docs/Phase 1 Initial Instructions - …dev_spec.md`).
§ references point into the master spec — use [spec-index.md](spec-index.md) for line
numbers. This digest is the default read; open the master spec only for section-level
detail.

## What we're building (§1–§4)

A **local-first niche-industry intelligence platform**. It discovers companies, projects,
events, and market signals from many imperfect public sources; preserves evidence and
provenance; resolves entities; classifies cautiously; scores confidence and freshness;
maps macro events to affected companies; and produces explainable, evidence-backed lead
queues. First industry: **scaffolding / temporary works (AU/NZ)** — delivered as a
configurable *industry pack*, never hard-coded into the core.

It derives intelligence from **agreement and disagreement between multiple imperfect
sources** (§3.1). It is explicitly *not* (§5): a CRM, a spam tool, a personal-data
harvester, a system that bypasses access controls, a scaffolding-only database, or a
single opaque AI pipeline.

## Guiding principles (§6) — condensed

1. **Evidence before assertion** — source document, observation, normalised fact,
   classification, inference, hypothesis, contradiction, and user decision are distinct
   records (§6.1).
2. **Accuracy is multidimensional** — authority, independence, extraction confidence,
   match confidence, freshness, corroboration, contradiction, specificity, completeness,
   human verification are assessed separately (§6.2).
3. **Missing ≠ false** (§6.3). Abstain rather than fabricate (§40).
4. **Facts are time-bound** — observed dates, valid-from/to, freshness policies (§6.4).
5. **Industry knowledge is configurable** — generic core concepts; packs define meaning
   (§6.5).
6. **Human correction is first-class** — overrides, preserved originals, reversible,
   feeds learning (§6.6).
7. **Collection is coordinated** — every collection has a purpose, budget, and health
   status (§6.7).
8. **AI operates on bounded tasks**; code owns fetching, hashing, matching, dates,
   arithmetic, scheduling, scoring mechanics, workflow state (§6.8, §26.2).
9. **No provider becomes structural** — AI optional, replaceable, logged,
   schema-constrained (§6.9).
10. **The system gains value with age** — history is the asset (§6.10).

## Architecture (§7, §29)

Five layers:
**A. Industry configuration** (packs) → **B. Acquisition** (APIs, bulk, feeds, HTML, PDF,
manual) → **C. Evidence** (immutable raw store) → **D. Intelligence processing** (parse,
resolve, classify, relate, detect change, score confidence, cluster events) →
**E. Commercial action** (queues, leads, watchlists, alerts, exports, API).

Data flow: Source → retrieval → **SourceDocument** (raw, hashed) → parse →
**Observation** (atomic, evidence-tied) → entity resolution → **FactAssertion**
(reconciled, scored) → profiles/graph → leads/queues/exports.

Baseline stack (§29.2, confirmed in ADRs): Python, FastAPI, PostgreSQL (+FTS, optional
vector), Pydantic, SQLAlchemy + migrations, Parquet/DuckDB for analytics later,
Playwright only where permitted, React frontend, Docker Compose. Local-first: binds to
localhost, secrets outside VCS, backup/restore built in (§29.3–§29.5, §32.1).

## Industry packs (§8)

A pack supplies: terminology, synonyms, exclusions, company archetypes, service/product
taxonomies, market segments, project types, buyer/decision roles, relationship and event
types, catalyst rules, source templates, discovery queries, classification rubrics,
lead-fit rules, size/operating-tier rules, evidence expectations, freshness policies,
reporting templates, seed entities. Versioned, reviewable, testable. Maps to formal
taxonomies (ANZSIC/NAICS/NACE…) but keeps its own operational taxonomy. The scaffolding
pack's ~45 archetypes and ~35 services (§8.4) are seeds, not restrictions.
**Acceptance guard: a second test industry must load without changing core tables (§35 M1, §41.18).**

## Core data model (§13) — mandatory conceptual distinctions

17 entities (YAML schemas in §13; line map in spec-index): Organisation, OperationalUnit,
Location, Person/RoleAssignment, ContactPoint, ClassificationAssignment,
Capability/CapabilityAssignment, Product/System, Project, ProjectParticipation, Event,
Relationship, SourceDocument, Observation, FactAssertion, ResearchQuestion,
AccountOpportunity. Full detail: [../architecture/data-model.md](../architecture/data-model.md).

Non-negotiables:
- Organisation ≠ OperationalUnit (legal entity vs branch/yard) — never flatten.
- Observation (what a source said) ≠ FactAssertion (what we conclude).
- Classification assignment types: registered / self-described / observed / inferred /
  human-confirmed / rejected.
- Capability statuses: claimed / evidenced / repeatedly-evidenced / verified /
  historical / uncertain / contradicted.
- FactAssertion statuses: confirmed / probable / possible / conflicted / stale /
  disproven / unknown.
- Everything carries evidence IDs, confidence, and time bounds.

## Sources & collection (§10–§12)

- **Source registry** (§10.2): every source has category, authority tier, robots/terms
  status, rate-limit policy, collection scope, health. Directories are *discovery*
  sources — weak signals generate hypotheses, not high-confidence facts (§10.1, §39.1).
- **Independence matters**: copied/syndicated content must not count as corroboration
  (§10.4, §17.5).
- **Responsible crawling (§11)**: prefer API > bulk > RSS > sitemap > HTML > rendered >
  manual. RFC 9309 robots compliance, clear user-agent identity, per-domain
  concurrency/delay/budgets, conditional requests (ETag/Last-Modified), content hashing,
  circuit breakers. **Never bypass** login, paywalls, CAPTCHAs, rate limits, or explicit
  prohibition (§11.4). Every frontier URL has purpose, priority, expected entity, depth,
  budget class (§11.6). Change detection retains history (§11.8).
- **Ingestion beyond crawling (§12)**: provider adapters for registries/procurement/news
  APIs (AU: ABR, ASIC; NZ: NZBN; UK: Companies House; GDELT, etc. — §43); bulk imports
  (CSV/JSON/XLSX/Parquet…) with full import provenance; PDF pipeline with page-level
  provenance, OCR only when necessary; manual evidence capture distinguishable from
  automated.

## Entity resolution (§14)

Match on registration numbers, names, domains, phones, addresses, officers, cross-links…
Match states: exact / high-confidence / possible-review / related-but-distinct /
confirmed-distinct / unresolved. **No destructive merge** — preserve originals, rationale,
splittability. Ambiguous cases go to a review queue sorted by commercial importance.
Conservative thresholds (§39.4).

## Classification, size, confidence (§15–§17)

- **Multi-axis** (§15.1): business model, service capability, sector exposure, project
  profile, geography, commercial maturity, delivery sophistication, customer profile.
  Never one category per company.
- **Explainable** (§15.3): every classification answers why, from which evidence, how
  fresh, what contradicts, claimed-vs-inferred, confidence.
- **Size = bands and tiers, never fabricated precision** (§16.1, §39.3). Separate
  estimates for legal entity vs group vs branch. Tier rubric (A strategic enterprise …
  D small operator, Unresolved) is pack-configurable (§16.4).
- **Confidence is field-level** (§17.1), composed from authority × extraction × match ×
  freshness × corroboration × specificity × contradiction (formula replaceable but
  inspectable, §17.2). Authority depends on the question (§17.3); freshness decay varies
  by field (§17.4). Contradictions are preserved, displayed, and spawn research
  questions (§17.6). Human-readable vocabulary: verified / high / moderate / low /
  speculative / conflicted / stale / unknown (§17.7).

## Leads & contacts (§19–§20)

Leads exist only relative to a configured **offering** (e.g. "scaffold design and
drafting outsourcing"). Separate scores: industry fit, service fit, scale, need
likelihood, timing, evidence quality, contactability, geographic fit, strategic value…
(§19.2). Need/negative/timing indicators are pack-configurable hypotheses (§19.3–§19.5).
**Every score produces reasons with evidence citations** (§19.6, §39.7). Outreach
briefings may be prepared but **never sent automatically** (§19.7). Contacts: prefer
role-based/general public business routes; no private-email inference presented as fact;
suppression (opt-out, do-not-contact, conflicts) respected everywhere including exports
(§20, §32.3).

## Events, macro signals, graph (§21–§23)

- **MarketSignal**: PESTLE-extensible domains, geography/sector scoped, direction,
  magnitude, confidence (§21.2–§21.3).
- **Macro→micro mapping**: signal → geography → sector → project type → buyer orgs →
  supplier orgs → service demand → lead/watchlist changes (§21.4).
- Market estimates always expose coverage, methodology, bias, confidence bands; indices
  never masquerade as economic values (§21.5–§21.6, §39.8).
- **Story clustering**: many articles → one story; track independent-origin count vs
  syndication (§22.1). Event taxonomy is pack-extensible (§22.2). EventImpact separates
  direct/likely/possible/unclear/contradictory effects (§22.3–§22.4). Events can raise
  lead priority, create research questions, trigger refreshes (§22.5).
- **Knowledge graph**: typed nodes/edges, direction, confidence, validity period,
  evidence. Relational implementation first; dedicated graph DB only if justified (§23.3).

## Jobs, quality, security (§30–§32)

- Job framework: observable, retryable, idempotent-where-practical, bounded, auditable;
  priority classes from interactive to maintenance; failing sources degrade in isolation
  (§30).
- Quality dashboard + audit trail of user edits, merges, rule runs, AI I/O, exports
  (§31). Version everything: packs, taxonomies, rules, parsers, prompts, formulas (§31.4).
- Local defaults: localhost binding, secrets outside VCS, no telemetry, local-only mode;
  minimal, public, B2B-appropriate personal data; field-level retention (§32).

## AI boundaries (§26)

Permitted: extraction, rubric classification, claim structuring, relationship hypotheses,
contradiction detection, summarisation, lead explanation, research-question generation,
translation. Code owns everything deterministic (§26.2). AI output must carry structured
schema, evidence refs, confidence, explicit unknowns, prompt/model versions, validation
(§26.3). Local/remote/hybrid/disabled modes (§26.5). Cache by content hash + prompt
version; deterministic filters first; small models for extraction (§26.6).
**No evidence, no confirmed fact** (§39.6).

## MVP (§36) and Definition of Done (§41) — abbreviated

MVP: runs locally; scaffolding pack loaded; source registry; responsible crawl; ≥1
official dataset import; raw evidence preserved; dedupe; profiles with evidence-backed
classification + tier; public contacts; research gaps; projects/relationships; limited
news/tender monitoring; explained lead queue for one offering; export; manual correction;
backup/restore.

DoD adds: second test pack loads without rework (18), AI can be disabled (19), macro
signals map to segments/orgs (11), merges reversible (5), architecture supports later
financial-market phases (20).

## Milestones (§35)

M0 Foundation → M1 Industry packs → M2 Source registry & evidence → M3 Crawler →
M4 Entity core & resolution → M5 Discovery & profiles → M6 Projects & graph →
M7 News/events/macro → M8 Lead intelligence → M9 Market workspace → M10 Actions &
integrations → M11 AI assistance → M12 Feedback & calibration.
Deliverables/acceptance distilled in [../roadmap.md](../roadmap.md).

## Named failure modes to design against (§39)

Directory pollution · marketing claims ≠ capability · size hallucination · merge damage ·
crawler misbehaviour · AI converting uncertainty to fact · opaque lead scores ·
misrepresented market stats · stale intelligence · scope collapse into CRM · scaffolding
hard-coded into core.
