# Conceptual Data Model

The spec (§13) mandates conceptual distinctions, not a physical schema. Field-level YAML
schemas for all entities are in spec §13 (line map: [../spec/spec-index.md](../spec/spec-index.md)).
This doc records the distinctions that must survive any physical design, plus enum
vocabularies. Physical schema decisions belong in ADRs/migrations.

## Entity inventory (spec §13.1–§13.17)

| Entity | One-liner |
|---|---|
| Organisation | Legal/trading entity, group, agency, or association; canonical + legal + trading names; parent links |
| OperationalUnit | Branch, yard, office, division of an Organisation — **never flattened into it** |
| Location | Address/geo with type (office, yard, project site, service-area centroid…) and geocode confidence |
| Person / RoleAssignment | Conservative PII; roles with seniority, dates, confidence |
| ContactPoint | Typed business contact route; `public_business_contact` + `role_based` flags; deliverability status |
| ClassificationAssignment | Entity × taxonomy category with assignment type, confidence, validity period, evidence |
| Capability / CapabilityAssignment | Pack-defined service capability with status ladder and evidence strength |
| Product/System | Generic: scaffold system, software, machine, method, standard, material |
| Project / ProjectParticipation | Projects with typed, confidence-scored participant roles |
| Event | Typed occurrence with geography, affected entities/markets, direction, magnitude, horizon |
| Relationship | Typed, directed, time-bound, evidence-backed edge between entities |
| SourceDocument | Immutable raw retrieval: hash, URLs, timestamps, access-policy snapshot, collector/parser versions |
| Observation | Atomic extracted statement: subject, predicate, object, source location, extraction method + confidence |
| FactAssertion | Reconciled conclusion with component scores and supporting/contradicting observation IDs |
| ResearchQuestion | Generated or manual gap/contradiction to investigate; prioritised, assignable |
| AccountOpportunity | Lead: organisation × offering with separate fit/timing/evidence/accessibility scores, reasons, unknowns |
| MarketSignal (§21.3) | Macro/sector signal with geography, sectors, direction, magnitude, confidence |
| EventImpact (§22.4) | Event → entity edge with impact type, direction, horizon, reasoning, evidence both ways |
| SourceDefinition (§10.2) | Registry entry: category, authority tier, robots/terms status, rate limits, health |
| SourceCoverage / SourceCoverageTarget | Correlated industry, geography, purpose, and pack-defined applicability dimensions (ADR-0010) |
| SourceIdentity / SourceRelationship | Canonical aliases plus ownership/copy/syndication lineage, separate from applicability |

## Mandatory distinctions (collapse = spec violation)

1. **Evidence chain**: SourceDocument (what was retrieved) → Observation (what it said)
   → FactAssertion (what we conclude). Three records, three lifecycles. Contradicting
   observations attach to the assertion — they are never overwritten (§17.6).
2. **Organisation vs OperationalUnit**: legal entity vs branch. Capabilities and
   contacts can differ per branch (§13.2).
3. **Group vs legal entity vs branch sizing**: separate size/tier estimates (§16.3).
4. **Claimed vs evidenced vs verified capability** (§13.7).
5. **Manual vs automated provenance**: user input is evidence with its own source type,
   distinguishable forever (§12.4, §6.6).
6. **Merge preserves originals**: merge rationale, prior identifiers, reversibility
   (§14.4).
7. **Time-bound everything**: observed_at, valid_from/valid_to, first/last_observed
   (§6.4).
8. **Source identity vs applicability vs independence**: one canonical source may have
   several correlated coverage profiles and several lineage edges. Never duplicate a
   source per industry, and never treat two industry associations as independent evidence
   (§10.3–§10.4; ADR-0010).

## Enum vocabularies (fixed by spec)

- **ClassificationAssignment.assignment_type**: registered · self-described · observed ·
  inferred · human-confirmed · rejected (§13.6)
- **CapabilityAssignment.capability_status**: claimed · evidenced · repeatedly-evidenced ·
  verified · historical · uncertain · contradicted (§13.7)
- **FactAssertion.status**: confirmed · probable · possible · conflicted · stale ·
  disproven · unknown (§13.15)
- **Entity-match states**: exact · high-confidence-probable · possible-review ·
  related-but-distinct · confirmed-distinct · unresolved (§14.3)
- **Confidence vocabulary** (display): verified · high · moderate · low · speculative ·
  conflicted · stale · unknown (§17.7)
- **Classification axes** (§15.1): business model · service capability · sector exposure ·
  project profile · operating geography · commercial maturity · delivery sophistication ·
  customer profile

## Confidence composition (§17.2 — formula replaceable, components mandatory & inspectable)

```
final_confidence = f(source_authority, extraction_confidence, entity_match_confidence,
                     freshness, corroboration, specificity, contradiction)
```

- Authority is question-relative (registry wins on legal status; current company page may
  win on current services) — §17.3.
- Freshness decay is field-specific (registration fast-refresh; project participation
  historical, no decay) — §17.4.
- Corroboration requires **independent origin** — syndicated/copied content doesn't count
  (§10.4, §17.5).

## Naming convention

Code and schema use the spec's British/Australian spellings for domain terms
(`Organisation`, `normalisation_status`, `canonical_name`) to stay greppable against the
spec. See [../conventions.md](../conventions.md).
