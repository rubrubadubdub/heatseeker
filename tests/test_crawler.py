"""M3 acceptance: disallowed never crawled; budgets hold; lineage retained;
changed pages preserve history; transitive discovery proposes, never crawls."""

import httpx
from heatseeker_common.db import session_scope
from heatseeker_source_registry.crawler import CrawlBudget, crawl_source
from heatseeker_source_registry.models import (
    CrawlFrontier,
    FrontierStatus,
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceDocumentReference,
    SourceLifecycle,
    TermsStatus,
)
from sqlalchemy import select, text

HOST = "https://scaffco.example"


def _site(pages: dict[str, str], robots: str = "User-agent: *\nAllow: /\n"):
    """Mock transport serving a small site; records every requested path."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path or "/"
        requested.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        body = pages.get(path)
        if body is None:
            return httpx.Response(404)
        content_type = "application/xml" if path.endswith(".xml") else "text/html"
        return httpx.Response(200, content=body.encode(), headers={"Content-Type": content_type})

    return httpx.MockTransport(handler), requested


def _make_source(session, pack_id=None, access_method="html") -> str:
    source = SourceDefinition(
        name="ScaffCo",
        source_category="first_party",
        base_url=f"{HOST}/",
        access_method=access_method,
        authority_tier=3,
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.ALLOWED,
        terms_status=TermsStatus.APPROVED,
        pack_id=pack_id,
    )
    session.add(source)
    session.flush()
    return source.id


def test_disallowed_paths_are_never_fetched(engine, settings):
    settings.robots_policy = "enforce"
    transport, requested = _site(
        {
            "/": '<a href="/public">ok</a> <a href="/private/secret">no</a>',
            "/public": "<p>fine</p>",
            "/private/secret": "<p>must never be fetched</p>",
        },
        robots="User-agent: *\nDisallow: /private/\n",
    )
    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session, settings, source_id, transport=transport, sleeper=lambda s: None
        )
        assert summary["blocked"] == 1
    assert "/private/secret" not in requested  # the acceptance guarantee
    with session_scope(engine) as session:
        blocked = session.scalars(
            select(CrawlFrontier).where(CrawlFrontier.status == FrontierStatus.BLOCKED)
        ).one()
        assert "/private/secret" in blocked.url
        assert blocked.outcome == "robots disallow"


def test_budget_and_depth_limits_hold(engine, settings):
    # A page ring that would crawl forever without budgets.
    pages = {"/": '<a href="/p1">1</a>'}
    for i in range(1, 50):
        pages[f"/p{i}"] = f'<a href="/p{i + 1}">next</a><p>content {i}</p>'
    transport, _ = _site(pages)
    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=transport,
            budget=CrawlBudget(max_pages=5, max_depth=2),
            sleeper=lambda s: None,
        )
        # Linear chain at depth cap 2: /, /p1, /p2 — p2's links are not extracted.
        assert summary["pages_fetched"] == 3
        assert summary["stopped"] == "frontier exhausted"
    with session_scope(engine) as session:
        depths = session.scalars(select(CrawlFrontier.depth)).all()
        assert max(depths) <= 2  # nothing enqueued past the depth cap

    # Page budget dominates when the frontier is wide.
    wide = {"/": "".join(f'<a href="/w{i}">w{i}</a>' for i in range(30))}
    for i in range(30):
        wide[f"/w{i}"] = f"<p>unique {i}</p>"
    transport2, _ = _site(wide)
    with session_scope(engine) as session:
        source2 = SourceDefinition(
            name="Wide",
            source_category="news",
            base_url=f"{HOST}/",
            access_method="html",
            lifecycle_status=SourceLifecycle.ACTIVE,
            robots_status=RobotsStatus.ALLOWED,
            terms_status=TermsStatus.APPROVED,
        )
        session.add(source2)
        session.flush()
        summary2 = crawl_source(
            session,
            settings,
            source2.id,
            transport=transport2,
            budget=CrawlBudget(max_pages=5, max_depth=2),
            sleeper=lambda s: None,
        )
        assert summary2["pages_fetched"] == 5
        assert summary2["stopped"] == "page budget reached"


def test_lineage_purpose_and_history_are_retained(engine, settings):
    transport, _ = _site({"/": '<a href="/about">about us</a>', "/about": "<p>v1</p>"})
    with session_scope(engine) as session:
        source_id = _make_source(session)
        crawl_source(session, settings, source_id, transport=transport, sleeper=lambda s: None)
    with session_scope(engine) as session:
        child = session.scalars(
            select(CrawlFrontier).where(CrawlFrontier.discovered_via == "link")
        ).one()
        assert child.parent_url == f"{HOST}/"
        assert child.depth == 1
        assert child.purpose == "collection"
        assert child.document_id is not None

    # Changed page => new document; original document remains (spec §11.8).
    # No manual re-queue: age the frontier past the recrawl window and the next
    # crawl re-opens it automatically (the real change-detection-over-time path).
    from datetime import timedelta

    from heatseeker_common.timeutil import utc_now

    transport2, _ = _site({"/": '<a href="/about">about us</a>', "/about": "<p>v2 changed</p>"})
    with session_scope(engine) as session:
        for row in session.scalars(select(CrawlFrontier)):
            row.fetched_at = utc_now() - timedelta(days=2)
    with session_scope(engine) as session:
        summary = crawl_source(
            session, settings, source_id, transport=transport2, sleeper=lambda s: None
        )
        assert summary["pages_fetched"] >= 2  # frontier auto-reopened
    with session_scope(engine) as session:
        about_docs = session.scalars(
            select(SourceDocument).where(SourceDocument.source_url == f"{HOST}/about")
        ).all()
        assert len(about_docs) == 2  # history preserved, nothing deleted


def test_external_vocabulary_links_become_proposals_not_crawls(engine, settings):
    transport, requested = _site(
        {
            "/": (
                '<a href="https://partner.example/services">scaffolding design partner</a>'
                '<a href="https://cats.example/">cat pictures</a>'
                "<p>home</p>"
            )
        }
    )
    with session_scope(engine) as session:
        source_id = _make_source(session, pack_id="scaffolding_anz")
        summary = crawl_source(
            session, settings, source_id, transport=transport, sleeper=lambda s: None
        )
        assert summary["proposed_sources"] == ["partner.example"]  # vocabulary-gated
    assert all("partner.example" not in p for p in requested)  # proposed, never fetched
    with session_scope(engine) as session:
        proposed = session.scalars(
            select(SourceDefinition).where(SourceDefinition.origin == "proposal")
        ).one()
        assert proposed.lifecycle_status == SourceLifecycle.PROPOSED
        assert proposed.authority_tier == 6
        assert "backlink from 'ScaffCo'" in proposed.notes


def test_sitemap_seeds_frontier(engine, settings):
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{HOST}/projects</loc></url><url><loc>{HOST}/contact</loc></url></urlset>"
    )
    transport, _ = _site(
        {
            "/": "<p>home</p>",
            "/sitemap.xml": sitemap,
            "/projects": "<p>projects</p>",
            "/contact": "<p>contact</p>",
        }
    )
    with session_scope(engine) as session:
        source_id = _make_source(session, access_method="sitemap")
        summary = crawl_source(
            session, settings, source_id, transport=transport, sleeper=lambda s: None
        )
        assert summary["new_documents"] >= 3  # home + both sitemap pages
    with session_scope(engine) as session:
        via = set(session.scalars(select(CrawlFrontier.discovered_via)))
        assert "sitemap" in via


def test_diminishing_returns_stops_stale_crawl(engine, settings):
    pages = {"/": "".join(f'<a href="/s{i}">s{i}</a>' for i in range(20))}
    for i in range(20):
        pages[f"/s{i}"] = "<p>identical page</p>"  # every page same content hash
    transport, _ = _site(pages)
    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=transport,
            budget=CrawlBudget(max_pages=30, max_depth=2, stale_streak_stop=4),
            sleeper=lambda s: None,
        )
        assert summary["stopped"] == "diminishing returns (stale streak)"
        assert summary["pages_fetched"] < 30  # organic ending before budget


def test_unreachable_robots_is_conservative(engine, settings):
    settings.robots_policy = "enforce"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return httpx.Response(200, content=b"<p>hi</p>", headers={"Content-Type": "text/html"})

    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=httpx.MockTransport(handler),
            sleeper=lambda s: None,
        )
        assert summary["pages_fetched"] == 0  # restricted when robots can't be read
        assert summary["blocked"] >= 1


def test_ignore_mode_treats_robots_as_advisory(engine, settings):
    settings.robots_policy = "ignore"
    transport, requested = _site(
        {"/": '<a href="/private/secret">private</a>', "/private/secret": "<p>evidence</p>"},
        robots="User-agent: *\nDisallow: /private/\n",
    )
    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=transport,
            sleeper=lambda _seconds: None,
        )
        assert summary["blocked"] == 0
        assert summary["pages_fetched"] == 2
    assert "/private/secret" in requested
    assert "/robots.txt" not in requested


def test_crawler_collects_documents_and_images_with_parent_context(engine, settings):
    requested: list[str] = []
    page = b"""
    <h1>Bridge renewal</h1>
    <figure><img src="/project.jpg" alt="Scaffold around bridge">
      <figcaption>North approach works, May 2026</figcaption></figure>
    <a href="/capability.pdf">Capability statement</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(200, content=page, headers={"Content-Type": "text/html"})
        if request.url.path == "/project.jpg":
            return httpx.Response(
                200, content=b"jpeg evidence", headers={"Content-Type": "image/jpeg"}
            )
        if request.url.path == "/capability.pdf":
            return httpx.Response(
                200, content=b"%PDF evidence", headers={"Content-Type": "application/pdf"}
            )
        return httpx.Response(404)

    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
        )
        assert summary["pages_fetched"] == 1
        assert summary["documents_fetched"] == 1
        assert summary["images_fetched"] == 1
    assert {"/", "/project.jpg", "/capability.pdf"} <= set(requested)

    with session_scope(engine) as session:
        references = list(
            session.scalars(
                select(SourceDocumentReference).order_by(SourceDocumentReference.ordinal)
            )
        )
        assert {reference.reference_kind for reference in references} == {"document", "image"}
        image = next(reference for reference in references if reference.reference_kind == "image")
        assert image.context["alt_text"] == "Scaffold around bridge"
        assert image.context["caption"] == "North approach works, May 2026"
        assert image.child_document_id is not None
        assert all(reference.decision == "fetched" for reference in references)
        purposes = set(session.scalars(select(CrawlFrontier.purpose)))
        assert {"collection", "document", "image"} <= purposes


def test_external_assets_are_recorded_but_not_fetched(engine, settings):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            content=b'<img src="https://cdn.example/project.jpg" alt="external">',
            headers={"Content-Type": "text/html"},
        )

    with session_scope(engine) as session:
        source_id = _make_source(session)
        crawl_source(
            session,
            settings,
            source_id,
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
        )
    assert all("cdn.example" not in url for url in requested)
    with session_scope(engine) as session:
        reference = session.scalars(select(SourceDocumentReference)).one()
        assert reference.decision == "external_not_fetched"
        assert reference.child_document_id is None


def test_redirect_cannot_escape_reviewed_source_hosts(engine, settings):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "scaffco.example":
            return httpx.Response(302, headers={"Location": "https://escape.example/evidence"})
        raise AssertionError("off-origin redirect was fetched")

    with session_scope(engine) as session:
        source_id = _make_source(session)
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
        )
        assert summary["blocked"] == 1
        row = session.scalars(select(CrawlFrontier)).one()
        assert row.status == FrontierStatus.BLOCKED
        assert "redirect blocked" in row.outcome
    assert all("escape.example" not in url for url in requested)


def test_worker_crawl_releases_sqlite_transaction_during_fetch(engine, settings):
    with session_scope(engine) as session:
        source_id = _make_source(session)

    def handler(request: httpx.Request) -> httpx.Response:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE source_definition SET updated_at = updated_at WHERE id = :id"),
                {"id": source_id},
            )
        return httpx.Response(
            200, content=b"<p>evidence</p>", headers={"Content-Type": "text/html"}
        )

    with session_scope(engine) as session:
        summary = crawl_source(
            session,
            settings,
            source_id,
            transport=httpx.MockTransport(handler),
            sleeper=lambda _seconds: None,
            release_between_fetches=True,
        )
        assert summary["pages_fetched"] == 1
