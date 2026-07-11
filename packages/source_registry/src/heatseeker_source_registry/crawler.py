"""Responsible crawler (M3, spec §11): frontier walk with per-URL robots, budgets,
politeness, sitemap ingestion, link extraction, and transitive source discovery.

Hard guarantees:
- A robots-disallowed URL is recorded as BLOCKED and never fetched (spec §35 M3).
- Budgets (pages, depth, new domains, stale streak) bound every crawl — the frontier
  never becomes "crawl the internet" (spec §11.6, §24.4).
- Every frontier row carries purpose + lineage; changed pages create new documents
  without deleting history (change detection via content hash, spec §11.8).
- External domains matching pack vocabulary become PROPOSED sources with lineage —
  pass 1 of the vetting funnel; they are never crawled in the same run
  (docs/architecture/source-discovery.md).
"""

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx
from heatseeker_common import audit
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from protego import Protego
from selectolax.parser import HTMLParser
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry import COLLECTOR_VERSION
from heatseeker_source_registry.distill import distill_document
from heatseeker_source_registry.fetch import FetchTooLargeError, fetch_url
from heatseeker_source_registry.identity import (
    SourceIdentityConflict,
    attach_identity,
    canonicalise_url,
    resolve_identities,
    url_identity,
)
from heatseeker_source_registry.models import (
    CrawlFrontier,
    FrontierStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
)
from heatseeker_source_registry.policy import activation_blockers, policy_snapshot
from heatseeker_source_registry.rawstore import store_bytes

_SKIP_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".gz",
    ".mp4",
    ".mp3",
    ".woff",
    ".woff2",
    ".ttf",
    ".exe",
    ".dmg",
)


@dataclass
class CrawlBudget:
    """Deterministic bounds; AI never overrides these (ADR-0008)."""

    max_pages: int = 30
    max_depth: int = 2
    max_new_domains: int = 10
    stale_streak_stop: int = 8  # consecutive non-novel pages that end the run

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "CrawlBudget":
        values = {
            "max_pages": settings.crawl_max_pages,
            "max_depth": settings.crawl_max_depth,
            "max_new_domains": settings.crawl_max_new_domains,
            "stale_streak_stop": settings.crawl_stale_streak_stop,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass
class _RunState:
    pages_fetched: int = 0
    new_documents: int = 0
    unchanged: int = 0
    blocked: int = 0
    failed: int = 0
    proposed_sources: list[str] = field(default_factory=list)
    stale_streak: int = 0
    stopped_reason: str | None = None


class RobotsCache:
    """One robots.txt fetch per host per run; evaluated per URL path (RFC 9309)."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None):
        self._settings = settings
        self._transport = transport
        self._parsers: dict[str, Protego | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._parsers:
            self._parsers[host] = self._fetch(host)
        parser = self._parsers[host]
        if parser is None:  # robots unavailable (4xx/unreachable) => unrestricted
            return True
        agent = self._settings.crawler_user_agent.split("/")[0]
        return parser.can_fetch(url, agent)

    def _fetch(self, host: str) -> Protego | None:
        try:
            with httpx.Client(
                timeout=self._settings.fetch_timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self._settings.crawler_user_agent},
                transport=self._transport,
            ) as client:
                response = client.get(f"{host}/robots.txt")
            if response.status_code >= 500:
                # Server error: robots exists but is unreadable — restrict (RFC 9309).
                return Protego.parse("User-agent: *\nDisallow: /\n")
            if response.status_code >= 400:
                return None  # unavailable robots.txt (4xx) means unrestricted
            return Protego.parse(response.text)
        except httpx.HTTPError:
            # Conservative: if robots cannot be read due to server errors, treat the
            # host as restricted for this run rather than assuming permission.
            return Protego.parse("User-agent: *\nDisallow: /\n")


def _vocabulary_terms(session: Session, source: SourceDefinition) -> list[str]:
    """Pack vocabulary for gating discovery (terms + synonyms, lowercased)."""
    from heatseeker_industry_packs.loader import PackValidationError, default_packs_root, load_pack

    if not source.pack_id:
        return []
    try:
        pack = load_pack(default_packs_root() / source.pack_id)
    except (PackValidationError, FileNotFoundError):
        return []
    terminology = pack.files.get("terminology.yaml")
    if terminology is None:
        return []
    terms: list[str] = []
    for term in terminology.terms:
        terms.append(term.term.lower())
        terms.extend(s.lower() for s in term.synonyms)
    return terms


def _matches_vocabulary(text: str, terms: list[str]) -> bool:
    if not terms:
        return False
    lowered = text.lower()
    return any(term in lowered for term in terms)


def enqueue_url(
    session: Session,
    source: SourceDefinition,
    url: str,
    *,
    discovered_via: str,
    purpose: str = "collection",
    depth: int = 0,
    parent_url: str | None = None,
    priority: int = 50,
    discovery_rule: str | None = None,
) -> CrawlFrontier | None:
    """Add a URL to the frontier once per source (normalised); returns None on duplicate."""
    normalised = canonicalise_url(url)
    existing = session.scalars(
        select(CrawlFrontier.id).where(
            CrawlFrontier.source_definition_id == source.id,
            CrawlFrontier.normalised_url == normalised,
        )
    ).first()
    if existing:
        return None
    row = CrawlFrontier(
        source_definition_id=source.id,
        url=url,
        normalised_url=normalised,
        discovered_via=discovered_via,
        purpose=purpose,
        depth=depth,
        parent_url=parent_url,
        priority=priority,
        discovery_rule=discovery_rule,
    )
    session.add(row)
    session.flush()
    return row


def _parse_sitemap(content: bytes, base_url: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, nested_sitemap_urls); namespace-agnostic."""
    pages: list[str] = []
    nested: list[str] = []
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return pages, nested
    is_index = root.tag.split("}")[-1] == "sitemapindex"
    for loc in root.iter():
        if loc.tag.split("}")[-1] == "loc" and loc.text:
            target = urljoin(base_url, loc.text.strip())
            (nested if is_index else pages).append(target)
    return pages, nested


def _extract_links(base_url: str, raw: bytes) -> list[tuple[str, str]]:
    """(absolute_url, anchor_text) pairs from an HTML page."""
    links: list[tuple[str, str]] = []
    tree = HTMLParser(raw)
    for node in tree.css("a[href]"):
        href = node.attributes.get("href") or ""
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(("http://", "https://")):
            continue
        if urlsplit(absolute).path.lower().endswith(_SKIP_EXTENSIONS):
            continue
        links.append((absolute, node.text(strip=True) or ""))
    return links


def _propose_external_source(
    session: Session,
    origin_source: SourceDefinition,
    url: str,
    anchor_text: str,
    state: _RunState,
    budget: CrawlBudget,
) -> None:
    """Transitive discovery: an external, vocabulary-matched domain becomes a PROPOSED
    source with lineage — never crawled in this run (vetting funnel pass 1 input)."""
    if len(state.proposed_sources) >= budget.max_new_domains:
        return
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}/"
    identity = url_identity(root)
    try:
        existing = resolve_identities(session, [identity])
    except SourceIdentityConflict:
        return
    if existing is not None:
        return
    proposed = SourceDefinition(
        name=f"Discovered: {parts.netloc}",
        source_category="weak_signal",
        base_url=root,
        access_method="html",
        authority_tier=6,
        lifecycle_status=SourceLifecycle.PROPOSED,
        origin="proposal",
        pack_id=origin_source.pack_id,
        jurisdiction=origin_source.jurisdiction,
        geo_codes=origin_source.geo_codes,
        notes=(
            f"Auto-discovered via backlink from '{origin_source.name}' "
            f"(anchor: {anchor_text[:120]!r}, page: {url[:200]})"
        ),
    )
    session.add(proposed)
    session.flush()
    try:
        attach_identity(session, proposed, identity, origin="crawler", is_primary=True)
    except SourceIdentityConflict:
        session.expunge(proposed)
        return
    audit.record(
        session,
        "crawler",
        "source.proposed",
        "source",
        proposed.id,
        {"from_source": origin_source.name, "domain": parts.netloc, "anchor": anchor_text[:200]},
    )
    state.proposed_sources.append(parts.netloc)


def _store_page(
    session: Session,
    settings: Settings,
    source: SourceDefinition,
    url: str,
    result,
) -> tuple[SourceDocument | None, bool, bool]:
    """Persist a fetched page with dedupe; returns (document, is_new_row, novel_content).

    novel_content is hash-novelty across the whole source: twenty URLs serving the same
    boilerplate are one piece of material, not twenty (drives diminishing returns).
    """
    rel_path, digest = store_bytes(settings, result.content, result.content_type)
    hash_seen = (
        session.scalars(
            select(SourceDocument.id)
            .where(
                SourceDocument.source_definition_id == source.id,
                SourceDocument.content_hash == digest,
            )
            .limit(1)
        ).first()
        is not None
    )
    duplicate = session.scalars(
        select(SourceDocument).where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.source_url == url,
            SourceDocument.content_hash == digest,
        )
    ).first()
    if duplicate is not None:
        duplicate.last_seen_at = utc_now()
        duplicate.retrieval_count += 1
        return duplicate, False, False
    document = SourceDocument(
        source_definition_id=source.id,
        source_url=url,
        canonical_url=result.final_url if result.final_url != url else None,
        content_hash=digest,
        content_type=result.content_type,
        size_bytes=len(result.content),
        raw_storage_path=rel_path,
        http_status=result.status_code,
        etag=result.etag,
        last_modified=result.last_modified,
        access_policy_snapshot=policy_snapshot(source),
        targeting_snapshot={
            "schema_version": 1,
            "mode": "crawl",
            "coverage_ids": [],
            "coverages": [],
            "research_scopes": [],
        },
        collector_version=COLLECTOR_VERSION,
    )
    session.add(document)
    session.flush()
    distill_document(settings, document, result.content)
    return document, True, not hash_seen


def crawl_source(
    session: Session,
    settings: Settings,
    source_id: str,
    transport: httpx.BaseTransport | None = None,
    budget: CrawlBudget | None = None,
    sleeper=time.sleep,
) -> dict:
    """Crawl one source's site within budgets. Returns a run summary (job result)."""
    source = session.get(SourceDefinition, source_id)
    if source is None:
        return {"outcome": "error", "error": "source not found"}
    if source.lifecycle_status not in (SourceLifecycle.ACTIVE, SourceLifecycle.DEGRADED):
        return {"outcome": "skipped", "error": f"source is {source.lifecycle_status}"}
    blockers = activation_blockers(source)
    if blockers:
        return {"outcome": "blocked", "error": "; ".join(blockers)}
    if source.access_method == "manual" or not source.base_url:
        return {"outcome": "skipped", "error": "manual-only source"}

    budget = budget or CrawlBudget.from_settings(settings)
    state = _RunState()
    robots = RobotsCache(settings, transport)
    vocabulary = _vocabulary_terms(session, source)
    home_host = urlsplit(source.base_url).netloc

    # Re-open stale frontier rows so repeat crawls keep detecting change over time.
    # BLOCKED rows are re-queued too: robots is re-evaluated every run, so a still-
    # disallowed URL is re-blocked without ever being fetched.
    from datetime import timedelta

    recrawl_cutoff = utc_now() - timedelta(hours=settings.crawl_recrawl_hours)
    stale_rows = session.scalars(
        select(CrawlFrontier).where(
            CrawlFrontier.source_definition_id == source.id,
            CrawlFrontier.status != FrontierStatus.QUEUED,
        )
    ).all()
    for stale in stale_rows:
        last_touched = stale.fetched_at or stale.enqueued_at
        if last_touched < recrawl_cutoff:
            stale.status = FrontierStatus.QUEUED
            stale.outcome = None

    # Seed the frontier: base URL + declared endpoint + sitemap when relevant.
    seeds = {source.base_url}
    endpoint = (source.collection_scope or {}).get("endpoint_url")
    if endpoint:
        seeds.add(endpoint)
    for seed in seeds:
        enqueue_url(session, source, seed, discovered_via="seed", depth=0, priority=10)
    if source.access_method == "sitemap":
        enqueue_url(
            session,
            source,
            urljoin(source.base_url, "/sitemap.xml"),
            discovered_via="seed",
            purpose="sitemap",
            depth=0,
            priority=5,
        )

    first_request = True
    while state.pages_fetched < budget.max_pages:
        row = session.scalars(
            select(CrawlFrontier)
            .where(
                CrawlFrontier.source_definition_id == source.id,
                CrawlFrontier.status == FrontierStatus.QUEUED,
            )
            .order_by(CrawlFrontier.priority, CrawlFrontier.depth, CrawlFrontier.enqueued_at)
            .limit(1)
        ).first()
        if row is None:
            state.stopped_reason = "frontier exhausted"
            break
        if state.stale_streak >= budget.stale_streak_stop:
            state.stopped_reason = "diminishing returns (stale streak)"
            break

        if not robots.allowed(row.url):
            row.status = FrontierStatus.BLOCKED
            row.outcome = "robots disallow"
            state.blocked += 1
            continue

        if not first_request:
            sleeper(settings.politeness_delay_seconds)
        first_request = False

        try:
            result = fetch_url(settings, row.url, transport=transport)
        except (httpx.HTTPError, FetchTooLargeError) as exc:
            row.status = FrontierStatus.FAILED
            row.outcome = f"{type(exc).__name__}: {exc}"[:200]
            row.fetched_at = utc_now()
            state.failed += 1
            continue

        row.fetched_at = utc_now()
        state.pages_fetched += 1

        if result.status_code >= 400:
            row.status = FrontierStatus.FAILED
            row.outcome = f"HTTP {result.status_code}"
            state.failed += 1
            continue

        if row.purpose == "sitemap":
            pages, nested = _parse_sitemap(result.content, row.url)
            for nested_url in nested[:5]:
                enqueue_url(
                    session,
                    source,
                    nested_url,
                    discovered_via="sitemap",
                    purpose="sitemap",
                    depth=row.depth,
                    parent_url=row.url,
                    priority=5,
                )
            for page_url in pages[: budget.max_pages]:
                if urlsplit(page_url).netloc == home_host:
                    enqueue_url(
                        session,
                        source,
                        page_url,
                        discovered_via="sitemap",
                        depth=1,
                        parent_url=row.url,
                        priority=20,
                    )
            row.status = FrontierStatus.FETCHED
            row.outcome = f"sitemap: {len(pages)} urls, {len(nested)} nested"
            continue

        document, is_new, novel = _store_page(session, settings, source, row.url, result)
        row.status = FrontierStatus.FETCHED
        row.document_id = document.id
        row.outcome = "new content" if is_new else "unchanged"
        if is_new:
            state.new_documents += 1
        else:
            state.unchanged += 1
        if novel:
            state.stale_streak = 0
        else:
            state.stale_streak += 1

        # Link extraction: internal links extend the frontier; external, vocabulary-
        # matched domains become proposed sources (transitive discovery).
        if is_new and "html" in (result.content_type or "") and row.depth < budget.max_depth:
            for link_url, anchor in _extract_links(
                str(result.final_url or row.url), result.content
            ):
                link_host = urlsplit(link_url).netloc
                if link_host == home_host:
                    enqueue_url(
                        session,
                        source,
                        link_url,
                        discovered_via="link",
                        depth=row.depth + 1,
                        parent_url=row.url,
                        priority=50,
                    )
                elif _matches_vocabulary(f"{anchor} {link_url}", vocabulary):
                    _propose_external_source(session, source, link_url, anchor, state, budget)

    if state.stopped_reason is None:
        state.stopped_reason = "page budget reached"

    source.last_success_at = utc_now() if state.pages_fetched else source.last_success_at
    summary = {
        "outcome": "crawled",
        "source": source.name,
        "pages_fetched": state.pages_fetched,
        "new_documents": state.new_documents,
        "unchanged": state.unchanged,
        "blocked": state.blocked,
        "failed": state.failed,
        "proposed_sources": state.proposed_sources,
        "stopped": state.stopped_reason,
    }
    audit.record(session, "crawler", "source.crawled", "source", source.id, summary)
    return summary
