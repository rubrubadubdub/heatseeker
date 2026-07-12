# ADR-0014: Bounded agentic Source Scout with explicit unattended mode

**Status:** Accepted (owner directive) · **Date:** 2026-07-12 · **Amends:** ADR-0008

## Context

HeatSeeker already had deterministic backlink proposals, research scopes, responsible
collection, and an approved Codex/Claude provider direction, but source expansion still
required a user to search and register candidates. The owner directed that configured AI
connections should find and pull new sources from a control panel, including an optional
"let loose" mode.

The previous source-discovery design required human activation of every AI proposal
until M12 calibration. That prevents genuinely unattended plans, so this ADR narrows the
exception without weakening code-owned collection policy.

## Decision

- `packages/ai` owns task-level contracts, Codex/Claude subprocess adapters, versioned
  prompts, invocation audit, and persisted research plans/runs/proposals.
- Agent processes run in isolated scratch directories. Codex uses live search with a
  read-only ephemeral session; Claude receives only WebSearch and WebFetch. Prompts enter
  over stdin and output must validate against the source-expansion JSON schema.
- Scope, search parameters, instructions, budgets, provider, model, and activation mode
  are snapshotted on every run. Domain/category/exclusion/confidence filters are applied
  again by deterministic code after AI output.
- `proposal_only` is the default. `auto_activate` is an explicit per-plan authorization:
  proposals are registered at authority tier 6, robots and prohibited terms remain hard
  gates, and only cleared sources enter the existing collection/crawl queue. Unreviewed
  terms stay visible and do not become an AI assertion of approval.
- Schedules never overlap for one plan. Time, result, turn, output, and cost limits are
  bounded where the provider exposes them; every call and transition is audited.

## Consequences

Source expansion can run without repetitive user searching while the deterministic
crawler remains the only component that stores source evidence. Provider credentials
stay in provider-managed storage. Native agent search does not provide a HeatSeeker
pre-call hook for a strict per-query counter, so time/turn/cost/result limits bound the
first implementation; a controlled search broker remains an option if exact query
accounting becomes necessary.

## Alternatives rejected

- Let the agent write directly to SQLite or crawl URLs itself: bypasses policy,
  provenance, deduplication, and budgets.
- Keep mandatory human activation for all plans: does not satisfy the explicit unattended
  operating mode.
- Scatter provider-specific calls through UI and worker code: violates ADR-0006.
