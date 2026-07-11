# Conventions

## Language & naming

- **Domain terms use the spec's British/Australian spellings** in code, schemas, and DB:
  `Organisation`, `OperationalUnit`, `normalisation_status`, `canonical_name`. Rationale:
  the spec's YAML schemas (§13) are the contract; grep-ability between code and spec
  matters more than dialect consistency with library code.
- Python: `snake_case` modules/functions, `PascalCase` classes, no abbreviations for
  domain concepts (`classification_assignment`, not `class_asgn`).
- DB: singular table names matching entity names (`organisation`, `source_document`).
- Enum values in code/DB exactly as the spec lists them (see
  [architecture/data-model.md](architecture/data-model.md#enum-vocabularies-fixed-by-spec)).

## Code

- Python 3.12+, typed (`mypy --strict` target for `core_domain`, pragmatic elsewhere).
- Ruff for lint + format, line length 100.
- Pydantic v2 models for all external boundaries (API, pack files, AI output, imports);
  SQLAlchemy models for persistence. Don't share one class for both.
- No AI SDK imports outside `packages/ai` (ADR-0006). No industry-specific vocabulary
  outside pack data (spec §39.11).
- Determinism rule (spec §26.2): anything computable — dates, hashes, matching mechanics,
  scores, workflow state — is code, never an AI call.

## Testing

- pytest; tests mirror `packages/` structure.
- Every milestone's acceptance criteria get automated tests where feasible; the
  fixture menagerie in spec §37.2 (duplicate names, stale sites, copied directories,
  contradictions, robots-disallow, malformed PDF…) is built incrementally — add the
  fixtures relevant to your milestone.
- Crawler tests never hit the network: fixture HTML/robots served locally.
- Prefer precision over coverage of classification: abstention is a valid, testable
  outcome (spec §37.4).

## Git — atomic commits & pushes policy (owner directive 2026-07-11)

**Atomic commits.** One logical change per commit — one feature, one fix, one doc
update. Never mix refactors with behaviour changes, or unrelated fixes with features.
If a change description needs the word "and" between unrelated clauses, split it.

- **Green gate**: `uv run pytest -q && uv run ruff check .` must pass before every
  commit. No commit with failing tests or lint (no `--no-verify`, ever).
- **Schema changes travel whole**: a model change + its Alembic migration + its tests
  land in the same commit — never a migration orphaned from the code that needs it.
- **Message format**: `type(scope): imperative summary` (≤72 chars).
  Types: `feat`, `fix`, `docs`, `test`, `chore`, `refactor`, `perf`.
  Scopes mirror packages/apps: `common`, `core-domain`, `packs`, `sources`, `crawler`,
  `worker`, `api`, `cli`. Body says *why* and cites spec §/ADR where relevant.
  AI-authored commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **Docs move with code**: roadmap/status/ADR updates belong in the commit that makes
  them true (or an immediately following `docs:` commit — never left uncommitted).

**Pushes.** Push to `origin main` after each completed unit of work — at minimum after
every milestone step and at the end of every working session. Never force-push `main`.
Tag milestone completions: `git tag -a m<N>-complete` (push with `--follow-tags`).

- Never commit: `.env`, `data/`, secrets, real crawl output, desktop artifacts.
- History note: commits `c992958..3ed173a` are the initial import of M0–M3, split by
  area for reviewability; they are not individually buildable. Everything after them
  follows this policy.

## Documentation maintenance (the token-economy contract)

- **Master specs are frozen.** Never edit them — spec-index line numbers depend on it.
- Update [roadmap.md](roadmap.md) status when milestone work starts/finishes.
- Create the milestone brief in `milestones/` when starting a milestone (M0 is the
  template): goal, deliverables, acceptance checklist, boundaries, spec refs.
- Deviating from spec or an ADR → write a superseding ADR first.
- Keep CLAUDE.md short; it's loaded every session. Link, don't inline.
- New long-lived knowledge (gotchas, source quirks, external-API behaviours) goes in the
  narrowest relevant doc, not CLAUDE.md.
