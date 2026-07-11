# M2 — Source Registry & Evidence Store

**Status:** Done — acceptance verified live 2026-07-11 (mirror in [../roadmap.md](../roadmap.md))
**Spec:** §35 M2 (lines 3013–3030); §10 (sources), §11.2–11.4 (identity/robots/terms), §13.13 (SourceDocument)
**Delivered by:** `packages/source_registry` + worker jobs + GUI workspaces (Sources, Evidence, Scope)

## What was built

- **SourceDefinition registry** with lifecycle (proposed→candidate→active↔degraded→
  disabled/rejected), robots + terms status, authority tier, health tracking. Pack seed
  lists sync in via `sources.sync_pack_seeds` (human review fields never clobbered).
- **Policy gate** (policy.py): robots.txt fetched and evaluated (Protego, RFC 9309
  4xx⇒unrestricted); activation blocked until robots allowed/N-A and terms not
  prohibited; **re-gated on every collection**. Deterministic code — AI never bypasses
  it (ADR-0008).
- **Collection** (collect.py): identified UA (`crawler_user_agent` setting), size cap,
  conditional GET (ETag/Last-Modified), content-addressed raw store
  (`data/raw/<h2>/<h4>/<sha256>`), per-source failure isolation, auto-degrade after 3
  consecutive failures + auto-restore on success.
- **SourceDocument** rows carry the access-policy snapshot they were collected under;
  identical re-fetches bump `retrieval_count` instead of duplicating; changed content
  creates a new document (history preserved).
- **Research scopes** (owner requirement): named geography sets (ANZ/APAC/Global
  defaults + custom like `AU-QLD, US-WA, AU-VIC-MELBOURNE`), one active at a time,
  hierarchical matching in `heatseeker_core_domain.geography` (national sources cover
  state scopes and vice versa; GLOBAL always matches). Sources page filters/badges by
  scope; **M5+ discovery/leads/markets must consume `scopes.active_scope()`**.
- **Contextual source coverage** (hardening, ADR-0010): canonical sources can be shared
  across packs while coherent coverage profiles pair industry + geography + extensible
  facets without false Cartesian matches. Coverage carries pack provenance, priority,
  confidence, validity, and contextual authority/configuration overrides. Stable source
  identities prevent shared feeds from being duplicated; source relationships separately
  preserve ownership/syndication/derivation lineage for independence checks.
- Coverage-specific endpoints have their own path-sensitive robots state. Endpoint edits
  reset policy to unknown; collection stores effective policy + coverage + research-scope
  snapshots. Database checks prevent cross-source coverage/document links and protect
  evidence from source deletion.
- **GUI**: Sources (sync/check-policy/terms-review/activate/collect), source detail,
  Evidence browser with raw-content preview + policy snapshot, Scope manager.
  Source workspace now includes canonical create/edit, faceted industry/region filters,
  pairing management, endpoint policy, stable identities, and lineage links. JSON adds
  source CRUD/detail/resolve, coverage CRUD/summary, multi-dimensional scopes, contextual
  collection, and document targeting fields. CLI: `heatseeker sources` / `scopes`.

## Acceptance (spec §35 M2) — all verified

- [x] Original evidence preserved — bytes on disk match hash; verified live with a real
      GDELT fetch (46 KB, `data/raw/2c/3f/2c3fe09d…`).
- [x] Duplicate retrieval recognised — live re-collect returned `duplicate`,
      retrieval_count=2, still one document.
- [x] Source failure isolated — failing source degrades itself after 3 failures; healthy
      sources unaffected (tests).
- [x] Source policy visible — robots/terms badges in GUI; policy snapshot stored on
      every document. Live robots checks: GDELT ✓ allowed, AusTender ✓ allowed.
- [x] Context tuples stay coherent — asymmetric industry/region profiles cannot leak into
      false cross-combinations; GLOBAL and unknown are distinct (automated tests).
- [x] Legacy M2 databases upgrade/downgrade without changing source/document ids or
      losing evidence; source/coverage/identity constraints verified in SQLite.

## Notes for later milestones

- M3 extends this into the full crawler (frontier, budgets, sitemaps, change detection);
  `fetch.py`/`rawstore.py` are the primitives to reuse.
- Parsing documents into Observations is M4/M5 (`parser_version` field reserved).
- Scope-aware discovery queries (pack `discovery_query_seeds` × scope regions) land at M5.
