# ADR-0006: AI behind a provider-agnostic, optional adapter

**Status:** Accepted, amended by [ADR-0008](0008-ai-in-the-loop-default.md) (AI-in-the-loop
default posture; agentic CLI providers) · **Date:** 2026-07-10

## Context

Spec §6.9: no model/provider may become structural. §26: AI handles bounded semantic
tasks only; output must be schema-constrained, evidence-citing, versioned, validated;
local/remote/hybrid/disabled modes; cache by content hash + prompt version. AI work is
M11, but earlier milestones must not paint us into a corner.

## Decision

- `packages/ai` exposes a **task-level interface** (e.g. `extract_claims(doc) ->
  ClaimExtraction`), not a chat interface. Callers never see providers or prompts.
- Each task defines: Pydantic output schema (with required evidence references and
  explicit-unknowns fields), prompt template (versioned file), validation rules,
  fallback behaviour when AI is disabled or output fails validation (skip + flag as
  research question — never fabricate).
- Providers implement one small interface (`complete(request) -> response`); config
  selects provider/model per task. `disabled` is a first-class provider.
- Every call is audited: input hash, prompt version, model, provider, raw output,
  validation result, cost. The audit table doubles as the cache (spec §26.6).
- **Nothing outside `packages/ai` may import an AI SDK.**

## Consequences

Deterministic pipelines (M0–M10) are built and tested AI-free by construction; M11
plugs into existing seams; provider swaps are config changes; the DoD "AI can be
disabled" (§41.19) holds trivially.

## Alternatives rejected

- **LangChain-style framework**: heavy dependency, obscures the audit/validation path
  the spec demands.
- **Direct SDK calls at point of use**: makes a provider structural — explicitly
  prohibited (§6.9).
