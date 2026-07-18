# ADR-0015 — Build M8 (lead intelligence) before M7 (news/events/macro)

Date: 2026-07-13 · Status: accepted

## Context

Spec §35 orders milestones M7 (news, events, macro signals) then M8 (lead
intelligence), but the dependency map (§35 sequencing, mirrored in the roadmap) only
requires M5 for M8 — M7 feeds M8's *timing* dimension, nothing structural. The
user's current business priority is producing ranked, explained, exportable lead
lists from the M5/M6 population.

## Decision

Implement M8 next; M7 follows. The lead engine ships with its timing dimension as an
honest neutral stub (constant score, explicit "timing signals land with M7" unknown on
every lead). When M7 lands, timing scoring plugs into the already-stored component
seam without schema changes. An XLSX export of the lead queue (spec §25.4/M10
territory) is pulled forward into M8 because the user needs list output now;
openpyxl is already a dependency and exports respect suppression from day one
(§32.3) — the M10 rule engine/REST surface remains M10.

## Consequences

- Lead timing scores are uninformative until M7; every lead says so in `unknowns`
  rather than pretending.
- Roadmap order deviates from §35; recorded here and in the roadmap table.
- M9 still waits for M7 (unchanged dependency).
