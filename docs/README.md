# Documentation Index

Entry point for all project documentation. Read [CLAUDE.md](../CLAUDE.md) first if you
haven't.

## Master specifications (frozen inputs — do not edit, do not read whole)

| File | What it is | How to use |
|---|---|---|
| `Phase 1 Initial Instructions - niche_industry_intelligence_phase_1_dev_spec.md` | Authoritative Phase 1 product spec (3,655 lines) | Targeted reads only, via [spec/spec-index.md](spec/spec-index.md) |
| `local_market_catalyst_intelligence_dev_spec.md` | Future-phase (financial-market) spec (2,709 lines) | **Not Phase 1 scope.** Context only — Phase 1 architecture must not preclude it (spec §1, §41.20) |

## Derived documentation (maintained — keep current)

| Doc | Purpose |
|---|---|
| [spec/spec-digest.md](spec/spec-digest.md) | Condensed Phase 1 requirements — the default substitute for the master spec |
| [spec/spec-index.md](spec/spec-index.md) | Section → line-number map for targeted spec reads |
| [roadmap.md](roadmap.md) | Milestones M0–M12 with acceptance criteria and current status |
| [architecture/overview.md](architecture/overview.md) | System layers, repo layout, data flow |
| [architecture/data-model.md](architecture/data-model.md) | Mandatory conceptual entities and distinctions |
| [architecture/decisions/](architecture/decisions/README.md) | Architecture Decision Records |
| [architecture/source-discovery.md](architecture/source-discovery.md) | Source registry lifecycle, expandable seed lists, AI-assisted source expansion |
| [conventions.md](conventions.md) | Code, testing, git, documentation conventions |
| [glossary.md](glossary.md) | Domain vocabulary |
| [milestones/](milestones/) | Per-milestone implementation briefs (created as each milestone starts) |

## Reading routes by task

| Task | Read (in order) |
|---|---|
| Starting any milestone | roadmap.md → milestones/M*.md → linked spec sections via spec-index |
| Designing a schema/model | architecture/data-model.md → spec §13 (targeted) |
| Building collectors/crawler | spec-digest §Source strategy + §Responsible crawling → spec §10–§12 (targeted) |
| Entity resolution work | glossary.md → spec §14 (targeted) |
| Classification/scoring work | architecture/data-model.md → spec §15–§17 (targeted) |
| Lead/event/market features | spec-digest → spec §19, §21–§22 (targeted) |
| Anything AI-related | spec-digest §AI boundaries → ADR-0006 → spec §26 (targeted) |
| Choosing a library/tool | architecture/decisions/ → spec §29 (targeted) |
| Writing tests | conventions.md → spec §37 (targeted) |

## Maintenance rules

- Master specs are immutable. If reality must diverge from the spec, write an ADR and
  note it in the relevant milestone brief — do not touch the spec.
- Update `roadmap.md` status when milestone work starts/completes.
- Create `milestones/M<N>-<name>.md` when a milestone begins (M0's exists as the template).
- If a derived doc contradicts a master spec without an ADR justifying it, the spec wins.
