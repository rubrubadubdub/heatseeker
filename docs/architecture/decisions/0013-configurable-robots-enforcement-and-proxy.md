# ADR-0013: Configurable robots enforcement and outbound fetch proxy

**Status:** Accepted · **Date:** 2026-07-12 · **Amends:** ADR-0011

## Context

Heatseeker is a private, low-volume research tool rather than a public search index.
Treating every `robots.txt` disallow as an unconditional legal or licence decision left
useful public evidence inaccessible, even though the specification explicitly separates
robots signalling from terms and legal authorisation. The owner directed that robots be
advisory by default while keeping collection identifiable, polite, bounded, and audited.
Some sources also require region-appropriate network egress.

## Decision

- `Settings.robots_policy` is `ignore` by default and accepts `ignore` or `enforce`.
- `SourceDefinition.respect_robots_override` is tri-state: `NULL` inherits the global
  setting, true always enforces, and false always treats robots as advisory.
- Robots is still fetched, displayed, rechecked, and recorded with every evidence item.
  Provenance records both the observed status and whether it was enforced or overridden.
- Terms prohibitions, authentication/paywall/CAPTCHA boundaries, identified user-agent,
  rate limits, crawl budgets, delays, conditional requests, and source lifecycle gates
  remain deterministic and are never disabled by this setting.
- `Settings.fetch_proxy_url` optionally routes policy checks, collection, crawler fetches,
  and robots requests through one HTTP(S) or SOCKS5 proxy. Injected test transports win.
  Secrets belong in environment configuration and are never written to evidence/audit
  payloads.
- Activation and collection evaluate the effective policy consistently in the worker,
  autopilot, GUI, JSON API, and CLI. Per-source changes are audited.

## Consequences

Operators can use public pages that opt out of indexing without pretending robots grants
or denies legal permission. Evidence remains explicit about the choice. One static proxy
is only a routing seam: automatic VPN selection, health checks, and per-region switching
remain tracked work and must not become rate-limit or access-control evasion.

## Alternatives rejected

- **Always enforce robots:** inappropriate as the only operating mode for the owner's
  private research workflow and conflates a crawler preference with legal permission.
- **Never inspect robots:** loses valuable provenance and prevents stricter per-source use.
- **Scattered direct proxy configuration:** risks policy checks and evidence fetches using
  different egress paths; one shared helper keeps behavior coherent.
