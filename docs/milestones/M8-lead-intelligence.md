# M8 — Lead Intelligence (built before M7 — ADR-0015)

Spec: §13.17 (AccountOpportunity), §19 (lead intelligence), §20 (contact priority),
§32.3 (suppression), §35 M8. Timing signals (§19.5) stay a declared stub until M7.

## Deliverables (spec §35 M8)

Offering definitions · lead scoring · timing signals (stub) · negative indicators ·
contactability · lead queue · account brief · suppression. Plus (pulled forward from
M10, ADR-0015): XLSX export of the queue with full profile columns.

**Acceptance:** leads exist per offering · scores have explanations · weak evidence
lowers confidence · suppression respected · no automatic outreach.

## Design decisions

1. **Package**: `packages/lead_intelligence` → `heatseeker_lead_intelligence`
   (planned layout). Migration 0018: `offering`, `account_opportunity`,
   `suppression_rule`.
2. **Offerings are DB rows, not pack files** — user-created and tunable in the UI
   (granularity preference), industry-agnostic core intact. An offering holds target
   archetypes, target capabilities, *need-gap* capabilities (absence ⇒ need
   hypothesis), negative archetypes, geo codes (defaults to the active scope), and
   JSON scoring weights with code defaults. Pack-shipped offering seeds can come later
   without schema change.
3. **Scoring is deterministic and explained** (§19.6): each §19.2 dimension scored by
   code from existing M5/M6 records — classifications (industry fit), capability
   ladder (service fit; verified > repeatedly-evidenced > evidenced > claimed),
   size tier (scale), fact confidence + profile completeness (evidence quality),
   §20.2 contact-route priority (accessibility), geography match, graph edges
   (existing-relationship risk note). Every dimension emits reasons with evidence
   references; absences emit `unknowns`, never fabricated scores (§6.3).
4. **Need-gap logic honours missing ≠ false**: a missing need-gap capability scores
   as a *hypothesis* ("no visible internal design team") and simultaneously records
   the uncertainty ("no reliable evidence of internal design staffing" — the spec's
   own example). Evidenced presence of the gap capability is a negative indicator.
5. **Weak evidence lowers priority structurally**: `commercial_priority` blends fit,
   timing, evidence quality, and accessibility; the evidence-quality component is
   multiplied in, so a high-fit company with thin evidence ranks below a
   well-evidenced peer (acceptance #3).
6. **Negative indicators** (§19.4): inactive/defunct/merged organisations are never
   scored; negative-archetype matches slash priority and add a risk; unresolved
   identity (open duplicate candidates) adds a risk.
7. **Suppression is a first-class rule** (§32.3): org-level suppression (opt-out /
   do-not-contact / existing-client / competitor / other) zeroes priority, moves the
   lead to stage `suppressed`, and **excludes it from every export**. Reversible with
   audit fields.
8. **XLSX export** (openpyxl, already a dependency): one workbook — `Leads` sheet
   with the full column set (scores, reasons, identifiers, domains, location,
   §20.2-ordered public contacts, capabilities with statuses, classifications, size
   bands, graph counts, provenance) and a `Method` sheet stating the formula, rule
   version, offering config, generation time, and the suppression guarantee.
   Suppressed organisations are absent, not blanked.
9. **No outreach**: the system stores a suggested next research action and factual
   brief inputs; it never sends anything (§19.7).
10. **Integration**: `leads.rescore` job; `pipeline.advance` rescores active
    offerings after profile refresh; dashboard checklist gains a "Define an offering
    & build the lead queue" step.

## Acceptance → test map

| Acceptance | Test |
|---|---|
| leads per offering | rescore creates AccountOpportunity per (org, offering); different offerings rank differently |
| scores explained | every dimension has reasons w/ evidence refs; unknowns include the timing stub |
| weak evidence lowers confidence | same fit, thinner evidence ⇒ lower commercial_priority |
| suppression respected | suppressed org: stage suppressed, priority 0, absent from XLSX |
| no automatic outreach | no send path exists; export is the only output |
