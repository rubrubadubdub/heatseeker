# M4 — Entity Core & Resolution

Spec: §13.1–§13.5 (entity schemas), §14 (resolution), §35 M4 (deliverables/acceptance).
Read [../architecture/data-model.md](../architecture/data-model.md) first — the
mandatory distinctions (Organisation ≠ OperationalUnit, merge preserves originals) are
acceptance criteria here.

## Deliverables (spec §35 M4)

Organisation · OperationalUnit · Location · Domain · Identifier · ContactPoint ·
merge + split · match scoring · resolution queue.

**Acceptance:** duplicates found and safely merged · branch/parent never flattened ·
ambiguous matches reviewable · every merge reversible.

## Design decisions

1. **Package**: new `packages/entity_resolution` → `heatseeker_entity_resolution`
   (models + matching + resolution together), matching the planned layout in
   [../architecture/overview.md](../architecture/overview.md). `core_domain` stays
   dependency-free, so SQLAlchemy models cannot live there (same precedent as
   `source_registry.models`).
2. **Merge is a pointer, not a rewrite** (§14.4). `perform_merge` sets
   `absorbed.status = "merged"` + `absorbed.merged_into_id = survivor` and writes an
   `entity_merge` audit row (rationale, signal snapshot, prior status). **No child rows
   move** — identifiers, domains, contacts, units stay attached to their original
   organisation. Reads resolve through `canonical_id()` (chain-following, cycle-guarded)
   and `merge_group()` aggregates the combined profile. Split = `reverse_merge`: restore
   prior status, clear pointer, stamp the audit row `reversed_at`. Nothing is ever
   deleted or overwritten, so reversal is exact.
3. **Match scoring is deterministic code** (ADR-0008 boundary): normalised signal
   comparison — shared identifier (scheme+value) → `exact`; shared domain or normalised
   legal-name equality → `high_confidence_probable`; fuzzy name (token overlap +
   SequenceMatcher) with locality/phone boosts → `possible_review`. Every candidate row
   stores the per-signal contribution list for explainability (§14.2).
4. **Nothing auto-merges.** Even exact matches enter `entity_match_candidate` and wait
   for a human decision (merge / related-but-distinct / confirmed-distinct). Queue sorts
   by score + conflicting-signal count (§14.5 priority, Phase-1 subset).
5. **Duplicate scan is blocked, not O(n²)**: candidate pairs come from shared blocking
   keys (identifier value, domain, normalised-name prefix token). Scan is idempotent —
   re-running updates scores in place, resolved pairs are not re-opened. Runs inline or
   as job `entities.match_scan`.
6. **Deferred from §13 deliberately**: `Person`/`RoleAssignment` (spec M4 list omits
   them; land with contact intelligence, §20), so `contact_point.person_id` is omitted
   until a `person` table exists. `website_domains`/`registration_identifiers` YAML
   lists become proper child tables (`organisation_domain`, `organisation_identifier`)
   because matching needs them indexed.

## Schema (migration 0012)

- `location` — address_lines JSON, locality/region/postal_code/country, lat/lon,
  location_type, geocode_confidence.
- `organisation` — canonical/legal/trading names, organisation_type, status
  (`active|inactive|defunct|merged|unknown`), country_of_registration, parent/ultimate
  parent FKs (self), primary_location FK, description, first/last_observed_at,
  profile_completeness, entity_confidence, provenance (`manual|ingestion`),
  merged_into_id FK (self).
- `operational_unit` — organisation FK, unit_type, name, location FK, service_area
  JSON, active_status, contact hint.
- `organisation_identifier` — organisation FK, scheme (abn/acn/nzbn/lei/…),
  value + normalised value (indexed), country, is_current, first/last_observed_at.
- `organisation_domain` — organisation FK, domain (normalised host, indexed),
  is_primary, first/last_observed_at. Unique per organisation.
- `contact_point` — organisation FK, operational_unit FK nullable, contact_type, value,
  label, public_business_contact, role_based, first_observed_at, last_verified_at,
  deliverability_status, confidence, source_evidence_ids JSON.
- `entity_match_candidate` — organisation_a/b FKs (a < b, unique pair), match_state
  (§14.3 vocabulary), score, signals JSON, resolution + resolved_by/at/notes,
  created/updated_at.
- `entity_merge` — survivor/absorbed FKs, candidate FK nullable, rationale,
  signals_snapshot JSON, absorbed_prior_status, performed_by/at, reversed_at/reason.

## Module map

| Module | Contents |
|---|---|
| `models.py` | tables + StrEnum vocabularies above |
| `normalise.py` | name (legal-suffix strip), domain, phone, identifier normalisation |
| `matching.py` | signal builders, `score_pair`, blocked `scan_for_duplicates` upsert |
| `resolution.py` | `canonical_id`, `merge_group`, `group_profile`, `perform_merge`, `reverse_merge`, `record_decision` |
| `entities.py` | create/list/get helpers used by API + later ingestion |
| worker `handlers/entities.py` | `entities.match_scan` job |
| API | `/entities`, `/entities/{id}`, `/resolution` pages + `/api/entities*`, `/api/resolution*` JSON |

## Acceptance → test map

| Acceptance | Test |
|---|---|
| duplicates found + safely merged | `test_matching.py` scan states; `test_resolution.py` merge keeps absorbed row + children intact |
| branch/parent not flattened | units/contacts stay on original org; group_profile lists per-org attribution; parent links never rewritten |
| ambiguous matches reviewable | possible_review candidates persist + decisions recorded, audit kept |
| every merge reversible | reverse_merge restores exact prior state; double-reverse and re-merge guarded |
