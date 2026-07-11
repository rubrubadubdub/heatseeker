# ADR-0011: Collection robustness — grading, auto-deprecation, politeness, economy

**Status:** Accepted (owner directive 2026-07-11) · **Extends:** ADR-0010

## Decisions

1. **Vetting & grading** (`grading.py`): every source gets a 0–100 score and A–E letter
   from observed evidence only — reliability (35), novelty yield (25), policy
   cleanliness (20), authority tier (20). No fetch history ⇒ grade "U" (abstain, §6.3).
   Components stored in `grade_detail` for inspectability (§17.2 spirit).
2. **Auto-deprecation** (deterministic, audited, reversible): robots-disallowed,
   terms-prohibited, persistently-failing (8+ consecutive, no success in 30d), or
   sustained grade E ⇒ lifecycle `deprecated` + reason + timestamp + audit record.
   `reinstate()` reverses to candidate. Multi-pass vetting: this deterministic pass is
   pass 1; an AI relevance review of survivors is pass 2 (M11, ADR-0008).
3. **Anti-blocking = politeness, never evasion** (§11.4 hard line): identified UA,
   conditional GETs, 429/503 + Retry-After honoured as `retry_after_until` (not counted
   as failure), jittered inter-request delays (doubled for same host), adaptive cadence
   (`schedule.py`: hourly floor, weekly ceiling; speeds up when content changes, backs
   off when it doesn't). Sources that still block us degrade toward manual workflows.
4. **Storage economy**: textual raw content gzip-compressed at rest (hash is always of
   original bytes; `.gz` path suffix marks encoding). Fetch size caps retained.
5. **Token economy**: every HTML/XML document is distilled to clean text
   (`distill.py`, selectolax; boilerplate stripped) under `data/processed/`;
   `GET /api/documents/{id}/text` is the preferred pipe for AI/agents (5–20x fewer
   tokens than raw), distilling on demand for older documents.
6. **Maintenance jobs**: `sources.evaluate_all` (grade + deprecate),
   `sources.recheck_policies` (robots re-evaluation after `robots_recheck_days`, §11.3),
   `sources.collect_due` (batch, polite). GUI: Collect due / Run maintenance buttons,
   grade badges, deprecated + reinstate flow.

7. **Autopilot** (owner directive: minimal human tuning): the worker enqueues a
   `sources.autopilot` job every `autopilot_interval_seconds` (default 5 min) — seed
   bootstrap when empty, bounded polite policy checks, auto-activation of cleared
   pack/user sources (proposals still wait for review), due collections, and daily
   maintenance. Every action audited; `autopilot_enabled=false` restores manual mode.

## Consequences

Source quality becomes measurable and self-pruning; collection stays block-resistant by
being a well-behaved client, not a disguised one; storage and downstream token costs
stay flat as evidence accumulates. Grading thresholds are constants pending M12
calibration against outcomes.
