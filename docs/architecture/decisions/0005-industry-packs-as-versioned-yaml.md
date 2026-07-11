# ADR-0005: Industry packs as versioned YAML directories

**Status:** Proposed · **Date:** 2026-07-10

## Context

Spec §8.2 lists pack contents (taxonomies, rubrics, rules, vocabularies, seeds…) and
requires the format be versioned, reviewable, and testable, leaving the concrete format
to the development agent. Packs will be edited by humans (and later via a UI, spec
§28.9) and validated by machines.

## Decision

- A pack is a **directory of YAML files** matching spec §8.2's content list, plus a
  `manifest.yaml` (pack id, name, semver version, spec/schema compatibility version).
- Every file validates against a **Pydantic schema** in `packages/industry_packs`;
  validation failures block loading (spec §35 M1 acceptance).
- Packs live in-repo under `packages/industry_packs/packs/<pack_id>/` and are
  git-versioned; the manifest version bumps on any content change. Loaded pack versions
  are recorded in the DB so intelligence is traceable to the pack version that produced
  it (spec §31.4, §33.5).
- The UI pack editor (M5+) writes the same YAML through the same validation.

## Consequences

Human-diffable, git-reviewable domain config; a scaffolding-free test pack (fixture for
spec §41.18) is just another directory; no bespoke DSL to maintain.

## Alternatives rejected

- **Database-only pack storage**: loses git review/versioning; harder to seed and test.
- **JSON**: no comments — pack rubrics need inline rationale.
- **Python-code packs**: violates the data/code seam; a malformed pack could crash core.
