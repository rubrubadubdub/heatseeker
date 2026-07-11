# Glossary

Domain vocabulary used throughout the spec, docs, and (eventually) code. Spec § refs in
parentheses.

- **Industry pack** — Versioned configuration bundle defining an industry's taxonomies,
  archetypes, rubrics, rules, vocabularies, and seeds. Domain knowledge lives here, not
  in core (§8).
- **Archetype** — Pack-defined organisation type (e.g. "industrial scaffold contractor",
  "temporary-works designer") (§8.4).
- **Source / SourceDefinition** — A registered place information comes from, with
  category, authority tier, robots/terms status, rate limits, and health (§10.2).
- **Source coverage** — One coherent rule describing where and why a source is useful,
  such as `scaffolding + AU + company discovery`. Dimensions within one coverage remain
  correlated; several coverages are alternatives. Missing geography is unknown, not
  automatically global (ADR-0010).
- **Source target** — One included or excluded value inside a source coverage. Core
  dimensions are industry and geography; industry packs may add purpose, market segment,
  project type, service, event type, and other namespaced dimensions.
- **Source identity** — A stable alias (pack seed key, canonical URL, or external id)
  that lets several packs refer to one canonical source without duplicating its policy,
  health, or evidence history.
- **Authority tier** — Question-relative trust ranking of a source; registry ≠ always
  best (§10.3, §17.3).
- **Source independence** — Whether two pieces of evidence share lineage (syndication,
  copying). Only independent evidence corroborates (§10.4).
- **Source relationship** — A time-bound ownership, syndication, copying, derivation,
  mirror, or successor edge between canonical sources. It models independence lineage;
  source coverage never substitutes for it.
- **SourceDocument** — One immutable retrieval: raw bytes (filesystem), hash, URLs,
  timestamps, access-policy snapshot, collector/parser versions (§13.13).
- **Observation** — Atomic extracted statement from one document: subject–predicate–
  object with location, method, extraction confidence. What a source *said* (§13.14).
- **FactAssertion** — Reconciled conclusion across observations, with component scores
  and both supporting and contradicting observation ids. What *we currently conclude*
  (§13.15).
- **Claim** — An assertion made by a company about itself (self-described); contrast
  *observed* and *verified* (§13.6–13.7).
- **Claimed vs evidenced vs verified capability** — Capability ladder: marketing copy →
  independent/project evidence → human confirmation (§13.7, §39.2).
- **Contradiction** — Coexisting conflicting observations; preserved, surfaced, and
  turned into research questions — never silently resolved (§17.6).
- **Freshness** — Field-specific time decay of confidence; distinct from correctness
  (§17.4).
- **Corroboration** — Confidence boost from independent, specific, temporally-consistent
  agreement (§17.5).
- **Entity resolution** — Deciding whether records refer to the same organisation,
  related entities, or distinct ones; conservative, queue-reviewed (§14).
- **Merge / split** — Reversible combination of duplicate entities preserving originals
  and rationale (§14.4).
- **Organisation vs OperationalUnit** — Legal/trading entity vs its branch/yard/office;
  never flattened (§13.1–13.2).
- **Operating tier** — Pack-configurable size/sophistication band (Tier A strategic …
  Tier D small, Unresolved); bands, never fabricated precision (§16).
- **Research question / research gap** — A generated or manual unknown worth
  investigating, prioritised and assignable (§13.16, §18.7).
- **Research plan** — Reusable, budgeted orchestration of discover→collect→resolve→
  classify→enrich→review (§24).
- **Offering** — A service we sell (e.g. scaffold design outsourcing); leads exist only
  relative to an offering (§19.1).
- **AccountOpportunity / lead** — Organisation × offering with separate fit/timing/
  evidence/accessibility scores, reasons, risks, unknowns (§13.17, §19).
- **Suppression** — Do-not-contact/opt-out/conflict flags that all queues and exports
  must respect (§32.3).
- **Event** — A discrete occurrence (award, insolvency, regulation…) with affected
  entities/markets, direction, magnitude, horizon (§13.11, §22).
- **Story clustering** — Grouping many articles into one underlying story, tracking
  independent origins vs syndication (§22.1).
- **Catalyst** — An event interpreted for its commercial consequences (§22.3).
- **MarketSignal** — Macro/sector-level development (PESTLE-extensible) scoped by
  geography and sector (§21.3).
- **Macro-to-micro mapping** — Signal → geography → sector → project type → buyers →
  suppliers → service demand → lead changes (§21.4).
- **Trend index** — Transparent proxy time-series (tender activity, hiring pressure…);
  never presented as a precise economic value (§21.6).
- **Crawl frontier** — Queue of URLs each carrying purpose, priority, expected entity,
  depth, budget class, expiry (§11.6).
- **Crawl budget** — Per-plan/domain limits on pages, bytes, time, AI calls (§11.5, §24.4).
- **Evidence expectation** — Pack-defined statement of what evidence a fact type
  requires before given statuses are allowed (§8.2).
- **Weak-signal source** — Discovery-grade source (directories, social, maps) allowed to
  generate hypotheses but not high-confidence facts (§10.1, §39.1).
