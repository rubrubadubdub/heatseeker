# industry_packs

Industry packs: versioned YAML data + (from M1) the loader/validation package
(ADR-0005). The pack **framework** (manifest schema, Pydantic validation, DB
registration) is an M1 deliverable — until then this directory holds pack *data* only.

- `packs/scaffolding_anz/` — first industry pack (spec §8.4).
  - `sources/seed_sources.yaml` — expandable seed list of research/scraping sources;
    lifecycle and AI-assisted expansion beyond the list are designed in
    [docs/architecture/source-discovery.md](../../docs/architecture/source-discovery.md).

Adding a source = adding a YAML entry (git-reviewed). Nothing in a seed list is crawled
until robots/terms checks pass at M2 (spec §11.4).

## Context-aware seed sources

`seed_sources/v1` remains supported and adapts each row to one coverage profile using
the declaring pack as its industry and `jurisdiction` as its region. New packs should use
`seed_sources/v2`, which separates a stable canonical source from one or more coherent
coverage tuples:

```yaml
schema: seed_sources/v2
pack: example_industry
sources:
  - key: shared_government_feed       # stable within this pack
    source_key: government_feed       # optional cross-pack canonical identity
    name: Government feed
    category: official_register
    url: https://data.example.gov/feed
    access: api
    authority_tier: 1
    language: en
    expected_update_frequency: PT1H
    authentication_type: api_key      # type only; never put secrets in a pack
    rate_limit_policy: {requests_per_minute: 30}
    coverages:
      - key: companies_au
        name: Australian company discovery
        industry_ids: [example_industry]
        region_codes: [AU]
        priority: 80
        relevance_score: 1.0
        confidence_score: 1.0
        include_targets:
          purpose: [company_discovery]
        exclude_targets:
          market_segment: [residential]
```

Each coverage is an OR alternative. Inside it, dimensions are AND-correlated, values in
one dimension are OR alternatives, and exclusions veto. Put `(industry A + AU)` and
`(industry B + NZ)` in separate coverage entries so they cannot leak into false
cross-combinations. Region targets default to symmetric `hierarchical` overlap; explicit
`exact`, `covers` (a national source covers a state request), and `within` (a local source
lies within a broader request) modes are also supported. See ADR-0010 and
[source-discovery.md](../../docs/architecture/source-discovery.md).

Pack sync never activates a source and never overwrites human lifecycle or terms review.
Changing an effective endpoint or access method resets the applicable robots decision.
Removing a seed disables its pack-origin coverage; it does not delete the canonical
source, identities, documents, or audit history.
