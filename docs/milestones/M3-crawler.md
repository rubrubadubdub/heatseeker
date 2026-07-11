# M3 — Responsible Crawler

**Status:** Done — acceptance verified 2026-07-11 (mirror in [../roadmap.md](../roadmap.md))
**Spec:** §35 M3 (lines 3031–3051); §11 (crawling), §24.4 (budgets)
**Delivered by:** `source_registry/crawler.py`, `CrawlFrontier` (migration 0007), job
`crawler.crawl_source`, UI crawl button

## What was built

- **Crawl frontier** (`crawl_frontier` table): every URL carries purpose, discovery
  route (seed/sitemap/link), depth, parent URL, priority, outcome, and resulting
  document id — full lineage per spec §11.6. Unique per source + normalised URL.
- **Per-URL robots** (`RobotsCache`): one robots.txt fetch per host per run, evaluated
  per path (RFC 9309 via Protego). 4xx robots ⇒ unrestricted; **5xx/unreachable ⇒
  restricted for the run** (conservative). Disallowed URLs are recorded BLOCKED and
  never fetched.
- **Budgets** (`CrawlBudget`, settings-backed, per-job overridable): max pages (30),
  max depth (2), max new domains (10), stale-streak stop (8). Novelty is
  content-hash-based across URLs, so boilerplate farms hit the diminishing-returns
  cutoff — the owner's "organic ending".
- **Sitemap support**: sitemap.xml + nested sitemapindex ingestion into the frontier
  (sources with `access_method: sitemap` seed it automatically).
- **Transitive backlink discovery**: external links whose anchor/URL matches pack
  vocabulary become **PROPOSED** sources with lineage in notes + audit
  (`source.proposed`), identity-deduplicated via the alias system, capped per run.
  They are never crawled in the same run — they enter the multi-pass vetting funnel
  (autopilot never auto-activates proposals).
- **Storage/pipeline reuse**: fetched pages go through the same rawstore (gzip),
  dedupe, and distillation as M2 collection; changed pages create new documents,
  history intact (§11.8).

## Acceptance (spec §35 M3) — verified

- [x] Disallowed paths are not crawled — mock-site test asserts the request never
      happened; frontier row records BLOCKED + reason.
- [x] Per-domain limits work — page/depth budgets + politeness delay between requests;
      wide-site test stops at exactly max_pages.
- [x] Crawl purpose and lineage retained — parent_url/depth/via/purpose on every row;
      verified live against GDELT (seed row, depth 0, fetched).
- [x] Changed pages create new documents without deleting history — re-crawl test
      yields two documents for the changed URL.

## Deferred

- Autopilot-scheduled crawls (currently button/job-triggered; wire into autopilot once
  crawl cadence policy is decided). Parser profiles beyond generic distillation → M5.
  Per-coverage crawl endpoints → when a real source needs it.
