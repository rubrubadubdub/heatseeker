# Heatseeker

Local-first **niche-industry intelligence platform**. Discovers, verifies, and organises
information about hard-to-research industries from many imperfect public sources, then
converts it into evidence-backed market insight and commercially actionable lead
intelligence. First industry pack: scaffolding, access, and temporary works (AU/NZ).

**Status:** M0–M2 complete: local runtime, industry packs, canonical source registry,
contextual industry/region/facet coverage, policy-gated collection, and immutable raw
evidence. See [docs/roadmap.md](docs/roadmap.md).

## Quick start

**Double-click `Heatseeker.bat`** (or the Heatseeker desktop shortcut). It migrates the
database, starts the worker and the web GUI in one window, and opens
http://127.0.0.1:8100/ in your browser. Close the window or Ctrl+C to stop.

Terminal equivalents:

```
uv run heatseeker run       # everything in one process (what the .bat runs)
uv run heatseeker serve     # API + GUI only
uv run heatseeker worker    # job worker only (separate terminal)
uv run heatseeker --help    # init, migrate, health, backup, jobs, packs, sources, scopes
uv run heatseeker sources --help
uv run pytest -q            # test suite
```

The browser GUI includes **Source Scout** at `/source-scout`: create bounded Codex or
Claude source-discovery plans, select a research scope/model, set search parameters and
budgets, schedule runs, and review proposals or explicitly enable policy-cleared
auto-collection. Provider credentials remain in the provider CLI's credential store.

Requires [uv](https://docs.astral.sh/uv/) — it provisions Python and dependencies
automatically on first run.

## Orientation

| You are… | Start at |
|---|---|
| An AI dev agent | [CLAUDE.md](CLAUDE.md) — reading rules + doc map |
| A human reader | [docs/README.md](docs/README.md) — doc index |
| Looking for requirements | [docs/spec/spec-digest.md](docs/spec/spec-digest.md) |
| Looking for the plan | [docs/roadmap.md](docs/roadmap.md) |

The authoritative Phase 1 specification lives in `docs/` (frozen input — see
[docs/spec/spec-index.md](docs/spec/spec-index.md) for a section map rather than reading
it whole).
