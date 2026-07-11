# ADR-0012: Named geography regions as data; scope exclusions

**Status:** Accepted · **Date:** 2026-07-11 · **Extends:** ADR-0010

## Context

The owner audit of research scopes (2026-07-11) found the geography layer pigeonholed in
three ways, even though scope *combinations* (mixing macro regions, countries, states,
and cities in one scope) already worked:

1. Macro regions (`APAC`, `EUROPE`, …) were a hardcoded Python dict — adding `LATAM` or
   `SOUTHEAST_ASIA` required a code change and release.
2. The country membership sets were not exhaustive, and a source tagged with an unlisted
   country silently fell outside the macro region that should contain it. With regions in
   code, incomplete membership was a *release* problem.
3. Scopes had no first-class geographic exclusions ("APAC except China"); users had to
   enumerate every wanted country instead.

## Decision

- **Named regions are data.** A `geo_region` table is the source of truth: `code`,
  display `name`, `member_codes` (countries or hierarchical codes — a
  `PACIFIC_NORTHWEST` of `US-WA, US-OR, CA-BC` is valid), `is_builtin`. Builtins
  (ANZ, APAC, NORTH_AMERICA, LATAM, EUROPE, MIDDLE_EAST, AFRICA) are seeded on first
  boot with broadened membership, remain user-editable, and cannot be deleted; new
  builtins added in later releases seed additively without touching user edits.
  Incomplete membership is now a data fix in the GUI, not a release.
- **`GLOBAL` stays special-cased** (matches everything), is not a table row, and cannot
  be redefined or excluded.
- **Regions may not nest.** Membership expansion stays single-level and predictable;
  a "region of regions" is expressed by listing the union of members.
- The core matcher (`heatseeker_core_domain.geography`) stays pure: it consults an
  in-process registry, replaced from the database at API/worker startup, after every
  region edit, and at each autopilot tick (so a separate-process worker converges within
  one tick). Pure unit tests fall back to the builtin defaults.
- Coverage checks are member-aware: a named region covers a code when any member is an
  ancestor of it, so subdivision members work in `covers`/`within`/overlap modes.
- **Scope exclusions carve out, they don't veto.** `ResearchScope.exclude_codes` drops a
  source only when its **entire known footprint** lies inside the excluded area
  (`geography.excluded_by`). "APAC minus China" keeps APAC-wide associations and a
  CN+AU multinational, and drops CN-only sources. For coverage-based sources the rule
  applies per coverage profile: profiles wholly inside the exclusion are ignored, and
  the source survives if any other profile matches. Unknown footprints are never
  excluded (missing ≠ false, spec §6.3); `include_unknown` governs those.
  The pre-existing coverage-level `target_filters` exclude polarity (ADR-0010) keeps its
  stricter overlap-veto semantics for power users; the two are documented separately.

## Consequences

- New table `geo_region` (migration 0008) and `research_scope.exclude_codes`
  (migration 0009). GUI: region editor on the scopes page; exclusion field on scope
  creation. API: `/api/regions` (GET/PUT/DELETE), `exclude_codes` on scope create.
- The registry is process-global mutable state — the one deliberate impurity. Test
  isolation restores builtins after every test (conftest autouse fixture).
- Deleting a region referenced by a scope or coverage target is refused with a clear
  message; sources keep working if a region code disappears (their stored codes simply
  stop expanding).
