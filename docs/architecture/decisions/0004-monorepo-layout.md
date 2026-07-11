# ADR-0004: Monorepo with applications/ + packages/ layout

**Status:** Accepted (validated by M0 implementation, 2026-07-10) · **Date:** 2026-07-10

## Context

Spec §29.1 sketches a monorepo separating deployable applications from importable
domain packages. The anti-goal is spec §39.11: scaffolding (or any industry) knowledge
leaking into the core.

## Decision

Adopt the layout in [../overview.md](../overview.md#repository-layout-planned-spec-291-baseline-adr-0003),
managed as a **uv workspace** (single lockfile, per-package `pyproject.toml`).

Dependency rules (enforced by review and, later, import-linter):

1. `packages/core_domain` imports only stdlib + Pydantic — no other project packages.
2. Industry packs are **data + a loader**; no package imports pack contents as code.
3. `applications/*` compose packages; packages never import applications.
4. `packages/common` holds shared utilities; anything domain-flavoured goes in a domain
   package instead.

## Consequences

Clear seams for the "second industry pack loads without core changes" acceptance test
(spec §41.18); packages are unit-testable in isolation; future extraction of the
financial-market phase can reuse evidence/event packages.

## Alternatives rejected

- **Single flat package**: faster to start, but the pack/core boundary — the spec's most
  emphasised architectural guard — becomes convention-only.
- **Multiple repos**: pointless overhead for a local single-team product.
