# Source Registry Expansion & AI-Assisted Source Discovery

How the platform knows *where* to research, how that list grows, and how AI proposes new
places to look (Reddit, forums, niche directories…) without violating collection rules.
Grounded in spec §9.1 (expand the source frontier), §10.1–§10.2 (source categories,
registry), §11.4 (terms/robots), §26 (bounded AI). Implemented across M2 (registry),
M3 (crawler), M5 (discovery workflows), M11 (AI proposals).

## 1. The expandable resources list

Three ways sources enter the registry, all converging on the same `SourceDefinition`
record (spec §10.2):

1. **Pack seed lists** — each industry pack ships `sources/seed_sources.yaml`
   (ADR-0005), a human-editable, git-reviewed list. Scaffolding pack seeds:
   [packages/industry_packs/packs/scaffolding_anz/sources/seed_sources.yaml](../../packages/industry_packs/packs/scaffolding_anz/sources/seed_sources.yaml).
   Add a source = add a YAML entry (or use the UI/CLI once M2 lands).
2. **User additions** — GUI and `heatseeker sources add/pair`: register a canonical
   URL/API, then attach one or more coherent coverage profiles.
3. **System proposals** — discovered candidates (see §3 below).

Canonical source identity is global. Pack seed keys and canonical-URL identities allow a
shared register, news feed, or directory to be referenced by several packs without
duplicating its policy, health, or evidence. Pack-specific usefulness lives in contextual
coverage profiles (ADR-0010), not `SourceDefinition.pack_id`.

### Contextual coverage

Each source has zero or more `SourceCoverage` profiles. A profile groups a meaningful
combination such as industry + geography + research purpose. Targets within a dimension
are alternatives, dimensions are conjunctive, exclusions veto, and profiles are
alternatives. This avoids false Cartesian matches:

```
(scaffolding AND AU) OR (coffee_roasting AND NZ)
```

does not match scaffolding/NZ. `GLOBAL` is explicit; no geographic target means unknown,
and each research scope chooses whether unknown coverage remains visible. Generic target
dimensions allow packs to add market segment, service, project type, event type, or other
facets without changing core tables. Coverage also carries pack provenance, contextual
priority, question-relative authority overrides, validity, and optional collection/parser
configuration.

If a profile overrides the collection endpoint, robots policy is checked and stored for
that profile rather than inherited from the canonical source. Endpoint changes reset the
check. Collection jobs carry the selected profile and a snapshot of the active research
scope so later edits cannot rewrite acquisition provenance.

Source ownership, copying, syndication, and derivation use `SourceRelationship`; those
edges determine independence and are intentionally separate from coverage.

## 2. Source lifecycle

```
proposed → candidate → (policy check: robots + terms review §11.4)
    → active → degraded (failures) → disabled
                          ↘ rejected (with reason, kept for audit)
```

- Every new source starts as **candidate** — nothing is crawled until the policy check
  passes and (for AI proposals) a human approves.
- Category defaults its **authority tier**: official register (1) … weak-signal (6–7).
  Weak-signal sources (social, forums, maps, Reddit) may *generate hypotheses and
  discovery leads, never high-confidence facts* (spec §10.1, §39.1).
- Health tracking (success/failure, change frequency) can degrade or retire sources
  (spec §30.4).

## 3. Expansion beyond the list — two mechanisms

### Transitive backlink discovery (owner requirement 2026-07-11, implemented at M3)

Sources spawn sources: when a permitted page (e.g. an association blog) links out,
matching outbound domains become `proposed` registry entries with lineage recorded
(which source/page proposed them, at what depth). Propagation continues transitively —
"spreading down the web" — but **bounded, never exhaustive-by-default**: depth caps,
per-plan page/domain budgets (spec §11.6, §24.4), pack-vocabulary gating (anchor/URL
text must hit industry terms), and a diminishing-returns cutoff (stop a branch when
new-domain yield falls below threshold — the "organic ending"). Every spawned candidate
then enters the multi-pass vetting funnel below.

### Multi-pass vetting funnel (owner requirement 2026-07-11)

1. **Pass 1 — deterministic (built, ADR-0011):** reachability, robots/terms policy,
   vocabulary relevance signals, and the grading engine (reliability/yield/policy/
   authority → A–E). Losers are auto-deprecated with reasons; audited and reversible.
2. **Pass 2 — AI review of survivors (M11, ADR-0008):** an AI rubric reviews graded
   candidates for actual relevance/quality, proposes coverage pairings and authority
   adjustments, with reasoning stored. AI never activates a source.
3. **Pass 3 — human approval** remains the activation gate (relaxable via config once
   M12 calibration shows the funnel is trustworthy).

### Deterministic expansion (code, M2–M5)

- Follow-on discovery from permitted crawls: association member directories,
  manufacturer distributor pages, project participant lists, sitemap discovery,
  outbound links matching pack vocabulary — each new domain becomes a *candidate
  source* with lineage recorded (which source/rule proposed it, why).
- Search-engine discovery queries generated from pack `discovery_queries` +
  synonym vocabulary (spec §8.2), rate-limited and budgeted.

### AI-assisted expansion (M11, seams reserved earlier)

AI proposes *where else to look*, using pack vocabulary and accumulated evidence:

- Suggest new source types/instances: subreddits (e.g. scaffolding/construction
  communities), trade forums, regional directories, government portals not yet
  registered, industry newsletters, YouTube/company channels.
- Generate targeted discovery queries for existing search sources.
- Flag coverage gaps ("no sources for WA region", "no tender portal for NZ").

**Guardrails (non-negotiable):**

1. AI output creates `proposed` registry entries only. Any later crawl is a separate,
   deterministic HeatSeeker transition under the plan's activation mode.
2. Robots is checked before unattended activation, and a recorded terms prohibition is
   always a blocker; sites known to prohibit automated access use `manual_only`
   workflows (spec §11.4). AI never marks terms as approved.
3. Human approval is the default for AI-proposed sources. An explicitly configured
   unattended research plan may activate policy-cleared proposals under ADR-0014; the
   choice, scope snapshot, and resulting crawl remain audited.
4. AI proposals carry reasoning + evidence references and land in the audit trail
   (spec §26.3).
5. Weak-signal defaults: authority tier 6–7, hypothesis-only fact policy.
6. Per-plan budgets cap discovery breadth (spec §24.4).

## 4. Where this lives in code

| Piece | Home | Milestone |
|---|---|---|
| `SourceDefinition` model + lifecycle states | `packages/source_registry` | M2 |
| Identity, contextual coverage + lineage relationships | `packages/source_registry` | M2 hardening |
| Seed-list loader (pack → registry sync) | `packages/industry_packs` + `source_registry` | M2 |
| Policy checker (robots fetch, terms status gate) | `packages/source_registry` | M2–M3 |
| Deterministic expansion rules | `packages/crawler` (frontier), `source_registry` | M3–M5 |
| `source.propose_expansion` AI task + Source Scout UI | `packages/ai` + API/worker (ADR-0014) | early M11 slice |
| Review queue for proposed sources | research queues (spec §28.8) | M5 |
