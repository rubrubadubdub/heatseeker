# M1 — Industry-Pack Framework

**Status:** Done — acceptance verified live 2026-07-11 (mirror in [../roadmap.md](../roadmap.md))
**Spec:** §35 M1 (lines 2993–3012); context §8 (pack system), ADR-0005
**Delivered by:** `packages/industry_packs` + `packs/scaffolding_anz/` data + CLI `packs` commands

## What was built

- **Schemas** ([schemas.py](../../packages/industry_packs/src/heatseeker_industry_packs/schemas.py)):
  strict Pydantic models (extra keys = errors) for manifest, terminology/synonyms/
  exclusions, company archetypes, service taxonomy, market segments, project types,
  event types, products/systems, and seed sources (incl. AI-expansion discovery config).
  `KNOWN_FILES` maps pack filenames → schemas; unknown files are rejected.
- **Loader** ([loader.py](../../packages/industry_packs/src/heatseeker_industry_packs/loader.py)):
  discovers packs under `packages/industry_packs/packs/` (env-overridable via
  `HEATSEEKER_PACKS_ROOT`), validates every YAML file, aggregates *all* problems into
  one `PackValidationError`, computes a SHA-256 content hash over the pack.
- **Registration** (models.py + registry.py): `industry_pack_registration` table
  (migration 0002) records pack id/name/version/content-hash; loads and version/content
  changes land in the audit trail (`pack.loaded` / `pack.updated`).
- **CLI**: `heatseeker packs list | validate <path> | load <pack_id>`.
- **Scaffolding pack v0.1.0** (`packs/scaffolding_anz/`): 45 archetypes, 33 services in
  8 categories, 16 segments, 10 project types, 15 event types, 13 systems, terminology
  with synonyms/exclusions, 21 seed sources + discovery config (spec §8.4 seeds).

## Acceptance (spec §35 M1)

- [x] A second test industry can be created without changing core tables —
      `tests/fixtures/packs/coffee_roasting` loads and registers through the same
      generic code path (test: `test_second_industry_pack_loads_without_core_changes`).
- [x] Pack changes are versioned — manifest semver + content hash recorded; updates
      audited (test: `test_reload_same_version_is_idempotent_and_change_is_audited`).
- [x] Pack validation catches invalid configuration — `bad_pack` fixture surfaces
      semver, id-mismatch, snake-case, and unknown-file errors in one aggregated report;
      CLI exits 1 (verified live).
- [x] Scaffolding categories are editable — plain YAML, git-reviewed; loader re-validates
      on load. (UI editor is a later milestone, spec §28.9.)

## Notes for later milestones

- M2 consumes `sources/seed_sources.yaml` to populate the SourceDefinition registry
  (sync job) per [../architecture/source-discovery.md](../architecture/source-discovery.md).
- Classification rubrics, lead-fit rules, size/operating-tier rules, freshness policies
  (spec §8.2) are deliberately deferred to the milestones that execute them (M5/M8) —
  add their schemas to `KNOWN_FILES` when they land; the loader/validation path is ready.
