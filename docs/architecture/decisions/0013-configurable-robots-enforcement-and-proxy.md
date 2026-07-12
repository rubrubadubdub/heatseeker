# ADR-0013: Configurable robots enforcement

**Status:** Accepted · **Date:** 2026-07-12 · **Amends:** ADR-0011

## Context

`robots.txt` is a crawler-coordination signal, which the specification deliberately keeps
distinct from a source's terms of use and from legal authorisation to access content.
Treating every `robots.txt` disallow as an unconditional, non-overridable block is the
right default, but it is occasionally too rigid: an operator may hold separate
authorisation to collect a specific endpoint that a site's blanket `robots.txt` opts out
of indexing. The system needs a safe default plus a narrow, auditable override, without
ever weakening the harder access boundaries.

## Decision

- `Settings.robots_policy` defaults to **`enforce`** and accepts `enforce` or `ignore`.
  `enforce` honours `robots.txt` Disallow rules; `ignore` treats them as advisory.
- `SourceDefinition.respect_robots_override` is tri-state: `NULL` inherits the global
  setting, `true` always enforces, and `false` treats robots as advisory for that source.
  This keeps any exception scoped, explicit, and reversible with a single field.
- Robots is always fetched, displayed, rechecked, and recorded with every evidence item.
  Provenance records both the observed status and whether it was enforced or overridden,
  so an advisory-mode collection is never silent.
- Terms prohibitions, authentication/paywall/CAPTCHA boundaries, the identified
  user-agent, rate limits, crawl budgets, politeness delays, conditional requests, and
  source lifecycle gates remain deterministic and are **never** disabled by this setting.
- Egress for policy checks, collection, crawler fetches, and robots requests is
  constructed through one shared helper (`http_client_kwargs`). Injected test transports
  take precedence; otherwise the default transport applies. Centralising egress keeps
  behaviour coherent and provides a single seam for future transport needs.
- Activation and collection evaluate the effective policy consistently in the worker,
  autopilot, GUI, JSON API, and CLI. Per-source changes are audited.

## Consequences

The default honours `robots.txt`, matching the project's responsible-collection posture.
Where an operator has separate authorisation, they can treat robots as advisory for a
specific source while every evidence item stays explicit about that choice. The shared
egress seam is transport plumbing only; it must never be used to evade access controls,
IP blocks, or rate limits, and any future transport selection must preserve that line.

## Alternatives rejected

- **Always enforce robots, no override:** too rigid where an operator holds separate,
  legitimate authorisation for a specific endpoint, and conflates a crawler-coordination
  signal with legal permission.
- **Never inspect robots:** loses valuable provenance and prevents stricter per-source use.
