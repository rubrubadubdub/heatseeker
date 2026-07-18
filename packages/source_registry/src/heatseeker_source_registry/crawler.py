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
from heatseeker_common.public_profiles import SocialProfile, try_social_profile_url
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from protego import Protego
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry import COLLECTOR_VERSION
from heatseeker_source_registry.distill import distill_document
from heatseeker_source_registry.document_pipeline import enqueue_document_processing
from heatseeker_source_registry.document_processing import detect_media_type
from heatseeker_source_registry.fetch import (
    FetchRedirectBlockedError,
    FetchTooLargeError,
    fetch_url,
    http_client_kwargs,
    response_filename,
)
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
    SourceDocumentReference,
    SourceLifecycle,
    SourceRelationship,
)
from heatseeker_source_registry.policy import activation_blockers, policy_snapshot, robots_enforced
from heatseeker_source_registry.publication import extract_claimed_published_at
from heatseeker_source_registry.rawstore import store_bytes
from heatseeker_source_registry.references import (
    REFERENCE_EXTRACTOR_VERSION,
    ReferenceCandidate,
    extract_references,
)


@dataclass
class CrawlBudget:
    """Deterministic bounds; AI never overrides these (ADR-0008)."""

    max_pages: int = 30
    max_depth: int = 2
    max_new_domains: int = 10
    stale_streak_stop: int = 8  # consecutive non-novel pages that end the run
    max_documents: int = 20
    max_images: int = 30
    max_images_per_page: int = 12

    @classmethod
    def from_settings(cls, settings: Settings, **overrides) -> "CrawlBudget":
        values = {
            "max_pages": settings.crawl_max_pages,
            "max_depth": settings.crawl_max_depth,
            "max_new_domains": settings.crawl_max_new_domains,
            "stale_streak_stop": settings.crawl_stale_streak_stop,
            "max_documents": settings.crawl_max_documents,
            "max_images": settings.crawl_max_images,
            "max_images_per_page": settings.crawl_max_images_per_page,
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
    documents_fetched: int = 0
    images_fetched: int = 0
    references_discovered: int = 0
    documents_queued: int = 0
    images_queued: int = 0
    proposed_sources: list[str] = field(default_factory=list)
    stale_streak: int = 0
    stopped_reason: str | None = None
    page_budget_hit: bool = False
    stale_limit_hit: bool = False


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
                **http_client_kwargs(self._settings, self._transport),
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


def _reference_context(candidate: ReferenceCandidate) -> dict:
    return {
        key: value
        for key, value in {
            "raw_url": candidate.raw_url,
            "source_attribute": candidate.source_attribute,
            "anchor_text": candidate.anchor_text,
            "alt_text": candidate.alt_text,
            "title_text": candidate.title_text,
            "caption": candidate.caption,
            "nearby_heading": candidate.nearby_heading,
            "declared_type": candidate.declared_type,
            "srcset_descriptor": candidate.srcset_descriptor,
        }.items()
        if value is not None
    }


def _record_and_enqueue_references(
    session: Session,
    source: SourceDefinition,
    parent: SourceDocument,
    row: CrawlFrontier,
    references: list[ReferenceCandidate],
    *,
    allowed_hosts: set[str],
    vocabulary: list[str],
    state: _RunState,
    budget: CrawlBudget,
) -> None:
    """Persist every DOM occurrence, then enqueue bounded same-origin targets."""
    for candidate in references:
        state.references_discovered += 1
        host = urlsplit(candidate.url).netloc
        decision = "external_not_fetched"
        child_id = None

        if host in allowed_hosts:
            purpose = candidate.kind if candidate.kind in {"document", "image"} else "collection"
            if candidate.kind == "navigation" and row.depth >= budget.max_depth:
                decision = "depth_limit"
            else:
                queued = enqueue_url(
                    session,
                    source,
                    candidate.url,
                    discovered_via="link",
                    purpose=purpose,
                    depth=row.depth + 1,
                    parent_url=row.url,
                    priority=40 if purpose == "document" else 70 if purpose == "image" else 50,
                    discovery_rule=candidate.rule,
                )
                decision = "queued" if queued is not None else "known"
                if queued is not None:
                    queued.expected_content = candidate.expected_content
                    if purpose == "document":
                        state.documents_queued += 1
                    elif purpose == "image":
                        state.images_queued += 1
                else:
                    existing = session.scalars(
                        select(CrawlFrontier).where(
                            CrawlFrontier.source_definition_id == source.id,
                            CrawlFrontier.normalised_url == candidate.normalised_url,
                        )
                    ).first()
                    child_id = existing.document_id if existing else None
        elif candidate.kind == "navigation" and (
            profile := try_social_profile_url(candidate.url)
        ) is not None:
            proposed = _propose_external_source(
                session,
                source,
                candidate.url,
                candidate.anchor_text or "",
                state,
                budget,
                discovered_on_url=parent.source_url,
                social_profile=profile,
            )
            decision = "source_proposed" if proposed else "known_external"
        elif candidate.kind == "navigation" and _matches_vocabulary(
            f"{candidate.anchor_text or ''} {candidate.url}", vocabulary
        ):
            proposed = _propose_external_source(
                session,
                source,
                candidate.url,
                candidate.anchor_text or "",
                state,
                budget,
                discovered_on_url=parent.source_url,
            )
            decision = "source_proposed" if proposed else "known_external"

        existing_reference = session.scalars(
            select(SourceDocumentReference).where(
                SourceDocumentReference.parent_document_id == parent.id,
                SourceDocumentReference.extractor_version == REFERENCE_EXTRACTOR_VERSION,
                SourceDocumentReference.reference_kind == candidate.kind,
                SourceDocumentReference.ordinal == candidate.ordinal,
            )
        ).first()
        if existing_reference is None:
            session.add(
                SourceDocumentReference(
                    parent_document_id=parent.id,
                    child_document_id=child_id,
                    target_url=candidate.url,
                    normalised_url=candidate.normalised_url,
                    reference_kind=candidate.kind,
                    discovery_rule=candidate.rule,
                    ordinal=candidate.ordinal,
                    context=_reference_context(candidate),
                    extractor_version=REFERENCE_EXTRACTOR_VERSION,
                    decision=decision,
                )
            )


def _link_references_to_child(
    session: Session, source: SourceDefinition, row: CrawlFrontier, child: SourceDocument
) -> None:
    for reference in session.scalars(
        select(SourceDocumentReference).where(
            SourceDocumentReference.child_document_id.is_(None),
            SourceDocumentReference.normalised_url == row.normalised_url,
        )
    ):
        parent = session.get(SourceDocument, reference.parent_document_id)
        if parent is not None and parent.source_definition_id == source.id:
            reference.child_document_id = child.id
            if reference.decision in {"queued", "known"}:
                reference.decision = "fetched"


def _propose_external_source(
    session: Session,
    origin_source: SourceDefinition,
    url: str,
    anchor_text: str,
    state: _RunState,
    budget: CrawlBudget,
    *,
    discovered_on_url: str,
    social_profile: SocialProfile | None = None,
) -> bool:
    """Transitive discovery: an external, vocabulary-matched domain becomes a PROPOSED
    source with lineage — never crawled in this run (vetting funnel pass 1 input)."""
    if len(state.proposed_sources) >= budget.max_new_domains:
        return False
    parts = urlsplit(url)
    root = social_profile.url if social_profile is not None else f"{parts.scheme}://{parts.netloc}/"
    identity = url_identity(root)
    try:
        existing = resolve_identities(session, [identity])
    except SourceIdentityConflict:
        return False
    if existing is not None:
        return False
    proposed = SourceDefinition(
        name=(
            f"Discovered {social_profile.platform} profile: {parts.path.strip('/')}"
            if social_profile is not None
            else f"Discovered: {parts.netloc}"
        )[:300],
        source_category="weak_signal",
        base_url=root,
        access_method="manual" if social_profile is not None else "html",
        authority_tier=6,
        lifecycle_status=SourceLifecycle.PROPOSED,
        origin="proposal",
        pack_id=origin_source.pack_id,
        jurisdiction=origin_source.jurisdiction,
        geo_codes=origin_source.geo_codes,
        notes=(
            f"Auto-discovered via backlink from '{origin_source.name}' "
            f"(anchor: {anchor_text[:120]!r}, page: {discovered_on_url[:200]})"
        ),
    )
    session.add(proposed)
    session.flush()
    try:
        attach_identity(session, proposed, identity, origin="crawler", is_primary=True)
    except SourceIdentityConflict:
        session.expunge(proposed)
        return False
    session.add(
        SourceRelationship(
            source_definition_id=proposed.id,
            related_source_definition_id=origin_source.id,
            relationship_type="discovered_via",
            confidence=1.0,
            origin="crawler",
            notes=f"Discovered on {discovered_on_url[:500]}",
        )
    )
    audit.record(
        session,
        "crawler",
        "source.proposed",
        "source",
        proposed.id,
        {
            "from_source": origin_source.name,
            "domain": parts.netloc,
            "profile_url": social_profile.url if social_profile is not None else None,
            "discovered_on_url": discovered_on_url[:500],
            "anchor": anchor_text[:200],
        },
    )
    state.proposed_sources.append(
        social_profile.url if social_profile is not None else parts.netloc
    )
    return True


def _store_page(
    session: Session,
    settings: Settings,
    source: SourceDefinition,
    url: str,
    result,
    *,
    enforce_robots: bool = True,
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
        enqueue_document_processing(session, settings, duplicate)
        return duplicate, False, False
    document = SourceDocument(
        source_definition_id=source.id,
        source_url=url,
        canonical_url=result.final_url if result.final_url != url else None,
        content_hash=digest,
        content_type=result.content_type,
        detected_content_type=detect_media_type(
            result.content,
            result.content_type,
            response_filename(result.content_disposition, result.final_url),
        ),
        content_disposition=result.content_disposition,
        original_filename=response_filename(result.content_disposition, result.final_url),
        claimed_published_at=(
            extract_claimed_published_at(result.content)
            if "html" in (result.content_type or "").lower()
            else None
        ),
        size_bytes=len(result.content),
        raw_storage_path=rel_path,
        http_status=result.status_code,
        etag=result.etag,
        last_modified=result.last_modified,
        access_policy_snapshot=policy_snapshot(
            source,
            collection_url=url,
            enforce_robots=enforce_robots,
        ),
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
    enqueue_document_processing(session, settings, document)
    return document, True, not hash_seen


def crawl_source(
    session: Session,
    settings: Settings,
    source_id: str,
    transport: httpx.BaseTransport | None = None,
    budget: CrawlBudget | None = None,
    sleeper=time.sleep,
    release_between_fetches: bool = False,
) -> dict:
    """Crawl one source's site within budgets. Returns a run summary (job result)."""
    source = session.get(SourceDefinition, source_id)
    if source is None:
        return {"outcome": "error", "error": "source not found"}
    if source.lifecycle_status not in (SourceLifecycle.ACTIVE, SourceLifecycle.DEGRADED):
        return {"outcome": "skipped", "error": f"source is {source.lifecycle_status}"}
    enforce_robots = robots_enforced(source, settings)
    blockers = activation_blockers(source, enforce_robots=enforce_robots)
    if blockers:
        return {"outcome": "blocked", "error": "; ".join(blockers)}
    if source.access_method == "manual" or not source.base_url:
        return {"outcome": "skipped", "error": "manual-only source"}

    budget = budget or CrawlBudget.from_settings(settings)
    state = _RunState()
    robots = RobotsCache(settings, transport)
    vocabulary = _vocabulary_terms(session, source)
    home_host = urlsplit(source.base_url).netloc
    allowed_hosts = {home_host}
    endpoint = (source.collection_scope or {}).get("endpoint_url")
    if endpoint:
        allowed_hosts.add(urlsplit(endpoint).netloc)
    for origin in (source.collection_scope or {}).get("allowed_origins", []):
        allowed_hosts.add(urlsplit(origin).netloc)

    def redirect_allowed(url: str) -> bool:
        return urlsplit(url).netloc in allowed_hosts and (not enforce_robots or robots.allowed(url))

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
    if release_between_fetches:
        session.commit()

    def checkpoint() -> None:
        if release_between_fetches:
            session.commit()

    first_request = True
    while True:
        row = session.scalars(
            select(CrawlFrontier)
            .where(
                CrawlFrontier.source_definition_id == source_id,
                CrawlFrontier.status == FrontierStatus.QUEUED,
            )
            .order_by(CrawlFrontier.priority, CrawlFrontier.depth, CrawlFrontier.enqueued_at)
            .limit(1)
        ).first()
        if row is None:
            state.stopped_reason = "frontier exhausted"
            break
        if row.purpose == "image" and state.images_fetched >= budget.max_images:
            row.status = FrontierStatus.SKIPPED
            row.outcome = "image budget reached"
            checkpoint()
            continue
        if row.purpose == "document" and state.documents_fetched >= budget.max_documents:
            row.status = FrontierStatus.SKIPPED
            row.outcome = "document budget reached"
            checkpoint()
            continue
        if row.purpose not in {"image", "document"} and state.pages_fetched >= budget.max_pages:
            row.status = FrontierStatus.SKIPPED
            row.outcome = "page budget reached"
            state.page_budget_hit = True
            checkpoint()
            continue
        if (
            row.purpose not in {"image", "document"}
            and state.stale_streak >= budget.stale_streak_stop
        ):
            row.status = FrontierStatus.SKIPPED
            row.outcome = "diminishing returns"
            state.stale_limit_hit = True
            checkpoint()
            continue

        row_id = row.id
        row_url = row.url
        row_purpose = row.purpose
        if release_between_fetches:
            session.rollback()

        if enforce_robots and not robots.allowed(row_url):
            if release_between_fetches:
                row = session.get(CrawlFrontier, row_id)
                if row is None:
                    continue
            row.status = FrontierStatus.BLOCKED
            row.outcome = "robots disallow"
            state.blocked += 1
            checkpoint()
            continue

        if not first_request:
            sleeper(settings.politeness_delay_seconds)
        first_request = False

        try:
            result = fetch_url(
                settings,
                row_url,
                transport=transport,
                max_bytes=(
                    settings.fetch_image_max_bytes
                    if row_purpose == "image"
                    else settings.fetch_document_max_bytes
                    if row_purpose == "document"
                    else settings.fetch_max_bytes
                ),
                redirect_validator=redirect_allowed,
            )
        except FetchRedirectBlockedError as exc:
            if release_between_fetches:
                row = session.get(CrawlFrontier, row_id)
                if row is None:
                    continue
            row.status = FrontierStatus.BLOCKED
            row.outcome = f"redirect blocked: {exc}"[:200]
            row.fetched_at = utc_now()
            state.blocked += 1
            checkpoint()
            continue
        except (httpx.HTTPError, FetchTooLargeError) as exc:
            if release_between_fetches:
                row = session.get(CrawlFrontier, row_id)
                if row is None:
                    continue
            row.status = FrontierStatus.FAILED
            row.outcome = f"{type(exc).__name__}: {exc}"[:200]
            row.fetched_at = utc_now()
            state.failed += 1
            checkpoint()
            continue

        if release_between_fetches:
            row = session.get(CrawlFrontier, row_id)
            source = session.get(SourceDefinition, source_id)
            if row is None or source is None:
                continue

        row.fetched_at = utc_now()
        if row.purpose == "image":
            state.images_fetched += 1
        elif row.purpose == "document":
            state.documents_fetched += 1
        else:
            state.pages_fetched += 1

        if result.status_code >= 400:
            row.status = FrontierStatus.FAILED
            row.outcome = f"HTTP {result.status_code}"
            state.failed += 1
            checkpoint()
            continue

        if row.purpose == "sitemap":
            document, is_new, _novel = _store_page(
                session,
                settings,
                source,
                row.url,
                result,
                enforce_robots=enforce_robots,
            )
            row.document_id = document.id
            _link_references_to_child(session, source, row, document)
            if is_new:
                state.new_documents += 1
            else:
                state.unchanged += 1
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
            checkpoint()
            continue

        document, is_new, novel = _store_page(
            session,
            settings,
            source,
            row.url,
            result,
            enforce_robots=enforce_robots,
        )
        row.status = FrontierStatus.FETCHED
        row.document_id = document.id
        _link_references_to_child(session, source, row, document)
        row.outcome = "new content" if is_new else "unchanged"
        if is_new:
            state.new_documents += 1
        else:
            state.unchanged += 1
        if row.purpose not in {"image", "document"}:
            if novel:
                state.stale_streak = 0
            else:
                state.stale_streak += 1

        # Link extraction: internal links extend the frontier; external, vocabulary-
        # matched domains become proposed sources (transitive discovery).
        if "html" in (result.content_type or ""):
            references = extract_references(
                str(result.final_url or row.url),
                result.content,
                max_images_per_page=budget.max_images_per_page,
            )
            _record_and_enqueue_references(
                session,
                source,
                document,
                row,
                references,
                allowed_hosts=allowed_hosts,
                vocabulary=vocabulary,
                state=state,
                budget=budget,
            )
        checkpoint()

    if state.stale_limit_hit:
        state.stopped_reason = "diminishing returns (stale streak)"
    elif state.page_budget_hit:
        state.stopped_reason = "page budget reached"
    elif state.stopped_reason is None:
        state.stopped_reason = "frontier exhausted"

    source = session.get(SourceDefinition, source_id)
    if source is None:
        return {"outcome": "error", "error": "source removed during crawl"}
    source.last_success_at = utc_now() if state.pages_fetched else source.last_success_at
    summary = {
        "outcome": "crawled",
        "source": source.name,
        "pages_fetched": state.pages_fetched,
        "new_documents": state.new_documents,
        "unchanged": state.unchanged,
        "blocked": state.blocked,
        "failed": state.failed,
        "documents_fetched": state.documents_fetched,
        "images_fetched": state.images_fetched,
        "references_discovered": state.references_discovered,
        "documents_queued": state.documents_queued,
        "images_queued": state.images_queued,
        "proposed_sources": state.proposed_sources,
        "stopped": state.stopped_reason,
    }
    audit.record(session, "crawler", "source.crawled", "source", source.id, summary)
    return summary
