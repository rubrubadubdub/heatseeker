# ADR-0016: Deterministic lead research loop with verified web lookup

**Status:** Accepted · **Date:** 2026-07-18 · **Amends:** ADR-0014, ADR-0015

## Context

Lead scoring admitted registry names with almost no usable company intelligence. The
website profiler only ran after a domain was already known and stopped permanently once
any contact existed. Consequently an ordinary public-web search could outperform the
export, while absent fields were merely listed as unknowns instead of driving research.

## Decision

- Code defines an industry-neutral completion contract: stable identity, official
  domain, specific location, public business contact, description, and evidenced
  relevance. Sparse matches are research candidates, not export-ready leads.
- HeatSeeker first generates a small bounded set of plausible hostnames from the exact
  company name and tries to verify them itself. If that produces no verified domain, a
  deterministic query plan is generated from exact names, registration identifiers,
  known geography, and current gaps. The existing bounded Codex/Claude web adapters may
  propose public URLs, but their output never becomes a fact directly.
- HeatSeeker fetches every proposed first-party URL itself under its normal robots,
  identity, byte, and provenance controls. A domain is attached only after deterministic
  on-page corroboration (name plus legal identifier/name, geography, or same-domain
  contact evidence). Ambiguity is retained as unknown.
- Once a domain is verified, a bounded breadth-first website pass prioritises pages for
  the fields still missing. Each pass has a hash of its gap set: new evidence unlocks the
  next pass; an unchanged exhausted pass is not repeated forever.
- The live queue shows incomplete accounts as `researching`. XLSX admits only completed
  accounts and reports the number still researching on its Method sheet.

## Consequences

The research controller is deterministic and auditable even though discovery uses live
web search. No bundled/offline company dataset or companion enrichment process is used.
Some genuinely unavailable facts remain unknown; the engine does not fabricate them or
silently attach a same-name website. Search-provider availability affects discovery
breadth, while known-domain collection still works with AI disabled.

## Alternatives rejected

- Export every scored name and label blanks as unknown: honest but commercially useless.
- Trust structured AI facts directly: fast, but bypasses evidence and identity controls.
- Guess domains from company names: deterministic but unsafe for same-name businesses.
- Retry every sparse profile on every pipeline tick: wasteful and has no organic ending.
