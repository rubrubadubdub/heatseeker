# ADR-0001: Record architecture decisions

**Status:** Accepted · **Date:** 2026-07-10

## Context

The master spec deliberately leaves implementation choices to the development agent
(§40) but requires meaningful deviations to be documented. Multiple AI agents will work
on this codebase across sessions; undocumented decisions get re-litigated, wasting
tokens and risking inconsistency.

## Decision

Record every significant architecture/tooling decision as an ADR in this directory,
following the process in [README.md](README.md). Baseline choices lifted from spec §29
enter as **Proposed**; the first milestone that exercises them promotes them to
**Accepted** or supersedes them.

## Consequences

Future agents check this index before choosing libraries, storage, or layouts, and
never re-open an Accepted decision without a superseding ADR.
