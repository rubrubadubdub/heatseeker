# ADR-0008: AI-in-the-loop by default; agentic CLI providers (amends ADR-0006)

**Status:** Accepted (user directive) · **Date:** 2026-07-10 · **Amends:** ADR-0006

## Context

Owner directive (2026-07-10): for market research, scraping, source discovery, and data
processing, AI in the loop is acceptable and expected — "assume AI in the loop will be a
must." Claude Code and Codex are approved as dependencies. ADR-0006's provider
abstraction, audit, and validation requirements are unchanged; what shifts is posture:
from "AI optional add-on at M11" to "AI assumed present, disableable for resilience."
Spec DoD §41.19 (AI can be disabled without breaking the core) remains binding — it is a
resilience property, not a design posture.

## Decision

1. **Default posture: AI-enabled.** Pipelines may assume a configured AI provider.
   When AI is disabled or output fails validation, the deterministic fallback is
   skip-and-flag (research question), never fabricate — same as ADR-0006.
2. **Provider set** (all behind the ADR-0006 task interface):
   - `anthropic` — direct API SDK; default for high-volume bounded tasks (extraction,
     classification against rubrics).
   - `claude-code` — subprocess adapter (`claude -p … --output-format json`); suited to
     *agentic research tasks*: multi-step source investigation, source-expansion
     proposals, deep company research where tool use (web search/fetch) helps.
   - `codex` — subprocess adapter (`codex exec …`); alternative agentic backend.
   - `disabled` — first-class no-op provider (spec §41.19).
   Per-task provider/model selection stays config-driven.
3. **AI seams may be exercised before M11** where they pay for themselves (e.g. M2
   source vetting, M5 discovery research). M11 remains the milestone where the full
   provider abstraction, audit UI, and validation harness are completed.
4. **Boundaries that stay code-owned regardless** (spec §26.2): robots.txt enforcement,
   rate limits, crawl budgets, scheduling, hashing, identifiers, arithmetic, workflow
   state. AI may *interpret* (e.g. an ambiguous terms-of-use page) and *propose*; the
   enforcement gate that acts on it is deterministic code + config.
5. **Audit still mandatory**: every AI invocation (SDK or CLI) logs input hash, prompt/
   task version, provider, model, raw output, validation result, and cost/tokens where
   reported (spec §26.3, §31.3).

## Consequences

`packages/ai` gains subprocess adapters alongside SDK adapters; research-type jobs can
delegate bounded investigations to an agentic CLI and receive schema-validated JSON
back. Setup gains an environment check: **neither `claude` nor `codex` was on PATH on
the target machine at the time of writing** — installing/exposing them is a documented
setup step before agentic providers activate (graceful degradation to `anthropic` or
`disabled` otherwise).

## Alternatives rejected

- **AI-optional posture everywhere** (prior reading of ADR-0006): contradicts the
  owner's stated operating assumption; leads to under-designed AI seams.
- **Direct SDK calls scattered at point of use**: still prohibited (§6.9) — the task
  interface is what makes providers swappable and auditable.
