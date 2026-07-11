# ADR-0010: Contextual source coverage profiles

**Status:** Accepted · **Date:** 2026-07-11 · **Extends:** ADR-0005, ADR-0007

## Context

The M2 registry stored an industry-pack id and geography summary directly on each
`SourceDefinition`. That works for one industry and one market, but it cannot safely
represent a shared source. Two independent many-to-many lists for industries and regions
would also be incorrect: a source relevant to scaffolding in Australia and coffee in New
Zealand would appear relevant to both false cross-combinations.

Source identity, contextual usefulness, and source lineage are different facts. A source
can be useful for several research contexts without becoming several independent sources,
and its authority can vary by question.

## Decision

- `SourceDefinition` remains the canonical registry identity and owner of global access
  policy, lifecycle, and health. Stable identities/aliases allow pack seeds and canonical
  URLs to converge on one definition.
- A `SourceCoverage` is one coherent applicability profile. Its targets are evaluated as
  **AND across dimensions**, **OR among included values in one dimension**, with matching
  exclusions taking precedence. Multiple profiles are OR alternatives. This preserves
  tuples such as `industry=scaffolding AND geography=AU`.
- `SourceCoverageTarget` uses extensible dimension keys. `industry` and `geography` are
  first-class conventions; pack taxonomies may add dimensions such as purpose, market
  segment, event type, service, or project type without a core-table migration.
- Geographic targets support exact, symmetric hierarchical, covers, and within matching.
  Generic targets use exact matching. Unknown coverage remains distinct from explicit
  `GLOBAL`; callers choose whether an unknown is retained through `include_unknown`.
- Coverage stores contextual priority, relevance/confidence, optional authority and
  collection/parser overrides, validity dates, and full pack provenance. Human policy
  review on the canonical source is never overwritten by a pack sync.
- A coverage that selects a different collection endpoint owns an independent robots
  decision. Collection is blocked until that exact endpoint path is checked; changing the
  endpoint resets the decision to unknown. Evidence snapshots the effective policy,
  coverage, and immutable research-scope context used by the queued job.
- `SourceRelationship` separately records ownership, copying, syndication, derivation,
  mirrors, and successor relationships. Coverage is never used as evidence independence.
- Existing v1 seed files adapt to one pack-industry + jurisdiction profile. Version 2
  seeds may declare stable identities and multiple explicit profiles. Removed seed
  profiles are disabled, not deleted; evidence and audit history remain intact.
- Legacy `pack_id` and `geo_codes` fields remain compatibility summaries while clients
  migrate to coverage-aware APIs.

## Consequences

Filtering several dimensions must correlate them through the same coverage profile.
Queries cannot join all industry targets and all geographic targets independently.
Collection records snapshot the coverage used so later target edits do not rewrite
provenance. Shared sources are displayed once and can carry several profiles.

The schema uses foreign keys (including coverage/source coherence on documents),
composite uniqueness, one-primary-identity and one-active-scope constraints, bounded
scores, stable keys, and disable-instead-of-delete workflows. Source deletion is
restricted while evidence exists. Matching is deterministic application code and is
covered by Cartesian-leak, global/unknown, policy, sync-idempotence, integrity, and
legacy-migration tests.

## Alternatives rejected

- **Industry and region tag lists on `SourceDefinition`:** cannot represent asymmetric
  combinations and causes false matches.
- **One duplicated source row per pack/region:** fragments policy and health, creates
  last-writer conflicts, and falsely inflates independent-source counts.
- **A fixed column for every taxonomy:** requires core migrations whenever an industry
  pack adds a targeting axis.
