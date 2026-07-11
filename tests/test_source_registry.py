"""M2 acceptance: seed sync, policy gate, evidence preservation, dedupe, failure isolation,
research scopes, geography matching."""

import httpx
import pytest
from heatseeker_common.db import session_scope
from heatseeker_core_domain.geography import in_scope, parse_jurisdiction
from heatseeker_industry_packs.loader import default_packs_root, load_pack
from heatseeker_source_registry import rawstore
from heatseeker_source_registry.collect import collect_source
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
    TermsStatus,
)
from heatseeker_source_registry.policy import activation_blockers, check_robots
from heatseeker_source_registry.scopes import (
    active_scope,
    create_scope,
    ensure_default_scopes,
    set_active,
    source_in_scope,
)
from heatseeker_source_registry.sync import sync_pack_seeds


def _make_source(session, **overrides) -> str:
    defaults = dict(
        name="Test Source",
        source_category="news",
        base_url="https://example.org/news",
        jurisdiction="AU",
        geo_codes=["AU"],
        access_method="html",
        authority_tier=4,
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.ALLOWED,
        terms_status=TermsStatus.APPROVED,
    )
    defaults.update(overrides)
    source = SourceDefinition(**defaults)
    session.add(source)
    session.flush()
    return source.id


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --- Seed sync ---------------------------------------------------------------


def test_sync_seeds_creates_candidates(engine):
    pack = load_pack(default_packs_root() / "scaffolding_anz")
    with session_scope(engine) as session:
        result = sync_pack_seeds(session, pack)
        assert result["created"] >= 20
    with session_scope(engine) as session:
        rows = session.query(SourceDefinition).all()
        assert all(r.lifecycle_status == SourceLifecycle.CANDIDATE for r in rows)
        assert all(r.origin == "pack_seed" for r in rows)
        abr = next(r for r in rows if "Australian Business Register" in r.name)
        assert abr.geo_codes == ["AU"]
        assert abr.authority_tier == 1

    # Re-sync is an update, not duplication; human review fields are preserved.
    with session_scope(engine) as session:
        row = session.query(SourceDefinition).first()
        row.terms_status = TermsStatus.APPROVED
        row_name = row.name
    with session_scope(engine) as session:
        result = sync_pack_seeds(session, load_pack(default_packs_root() / "scaffolding_anz"))
        assert result["created"] == 0
        assert result["updated"] >= 20
    with session_scope(engine) as session:
        row = session.query(SourceDefinition).filter_by(name=row_name).one()
        assert row.terms_status == TermsStatus.APPROVED  # not clobbered


# --- Policy gate ---------------------------------------------------------------


def test_activation_blocked_until_policy_cleared(engine):
    with session_scope(engine) as session:
        source_id = _make_source(
            session,
            lifecycle_status=SourceLifecycle.CANDIDATE,
            robots_status=RobotsStatus.UNKNOWN,
            terms_status=TermsStatus.UNREVIEWED,
        )
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        assert any("robots" in b for b in activation_blockers(source))
        source.robots_status = RobotsStatus.DISALLOWED
        assert any("disallow" in b for b in activation_blockers(source))
        source.robots_status = RobotsStatus.ALLOWED
        assert activation_blockers(source) == []
        source.terms_status = TermsStatus.PROHIBITED
        assert any("terms" in b for b in activation_blockers(source))


def test_check_robots_disallowed(engine, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /news\n")

    with session_scope(engine) as session:
        source_id = _make_source(session, robots_status=RobotsStatus.UNKNOWN)
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        status = check_robots(settings, source, transport=_transport(handler))
        assert status == RobotsStatus.DISALLOWED


def test_check_robots_missing_file_means_allowed(engine, settings):
    with session_scope(engine) as session:
        source_id = _make_source(session, robots_status=RobotsStatus.UNKNOWN)
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        status = check_robots(settings, source, transport=_transport(lambda r: httpx.Response(404)))
        assert status == RobotsStatus.ALLOWED


def test_collect_blocked_when_policy_regresses(engine, settings):
    """Even an ACTIVE source is re-gated at collection time."""
    with session_scope(engine) as session:
        source_id = _make_source(session, robots_status=RobotsStatus.DISALLOWED)
    with session_scope(engine) as session:
        result = collect_source(session, settings, source_id)
        assert result["outcome"] == "blocked"
        assert "robots" in result["error"]


# --- Evidence preservation and dedupe -------------------------------------------


def test_collect_preserves_original_evidence(engine, settings):
    body = b"<html><body>Tender awarded to Acme Scaffolding</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "text/html"})

    with session_scope(engine) as session:
        source_id = _make_source(session)
    with session_scope(engine) as session:
        result = collect_source(session, settings, source_id, transport=_transport(handler))
        assert result["outcome"] == "collected"
    with session_scope(engine) as session:
        doc = session.query(SourceDocument).one()
        assert doc.access_policy_snapshot["robots_status"] == "allowed"
        assert rawstore.read_bytes(settings, doc.raw_storage_path) == body  # bytes intact
        assert doc.content_hash == rawstore.content_address(body)


def test_duplicate_retrieval_recognised(engine, settings):
    body = b"same content every time"
    handler = _transport(lambda r: httpx.Response(200, content=body))
    with session_scope(engine) as session:
        source_id = _make_source(session)
    for _ in range(3):
        with session_scope(engine) as session:
            result = collect_source(session, settings, source_id, transport=handler)
    assert result["outcome"] == "duplicate"
    with session_scope(engine) as session:
        docs = session.query(SourceDocument).all()
        assert len(docs) == 1  # one document, not three
        assert docs[0].retrieval_count == 3


def test_changed_content_creates_new_document(engine, settings):
    responses = iter([b"version one", b"version two"])
    handler = _transport(lambda r: httpx.Response(200, content=next(responses)))
    with session_scope(engine) as session:
        source_id = _make_source(session)
    for _ in range(2):
        with session_scope(engine) as session:
            collect_source(session, settings, source_id, transport=handler)
    with session_scope(engine) as session:
        assert session.query(SourceDocument).count() == 2  # history preserved


# --- Failure isolation and health -----------------------------------------------


def test_source_failure_is_isolated_and_degrades(engine, settings):
    bad = _transport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("boom")))
    good = _transport(lambda r: httpx.Response(200, content=b"fine"))

    with session_scope(engine) as session:
        bad_id = _make_source(session, name="Bad Source")
        good_id = _make_source(session, name="Good Source", base_url="https://ok.example/x")

    for _ in range(3):
        with session_scope(engine) as session:
            result = collect_source(session, settings, bad_id, transport=bad)
            assert result["outcome"] == "failure"

    with session_scope(engine) as session:
        bad_source = session.get(SourceDefinition, bad_id)
        assert bad_source.lifecycle_status == SourceLifecycle.DEGRADED
        assert bad_source.consecutive_failures == 3
        # The healthy source is untouched and still collects.
        result = collect_source(session, settings, good_id, transport=good)
        assert result["outcome"] == "collected"

    # Success restores the degraded source.
    with session_scope(engine) as session:
        result = collect_source(session, settings, bad_id, transport=good)
        assert result["outcome"] in ("collected", "duplicate")
        assert session.get(SourceDefinition, bad_id).lifecycle_status == SourceLifecycle.ACTIVE


# --- Research scopes & geography -------------------------------------------------


def test_geography_scope_matching():
    assert in_scope(["AU"], ["AU-QLD"])  # national source covers state scope
    assert in_scope(["AU-QLD"], ["AU"])  # state source counts for national scope
    assert not in_scope(["NZ"], ["AU-QLD"])
    assert in_scope(["GLOBAL"], ["AU-QLD"])  # global sources always relevant
    assert in_scope(["AU", "NZ"], ["APAC"])  # macro region expansion
    assert not in_scope(["GB"], ["APAC"])
    assert in_scope(["US-WA"], ["US-WA", "AU-QLD"])
    assert in_scope([], ["AU"])  # unknown geo stays in scope (§6.3)
    assert parse_jurisdiction("AU/NZ") == ["AU", "NZ"]
    assert parse_jurisdiction("global") == ["GLOBAL"]


def test_scopes_default_create_activate(engine):
    with session_scope(engine) as session:
        ensure_default_scopes(session)
        scope = active_scope(session)
        assert scope.name == "ANZ"

        custom = create_scope(session, "QLD+WA+Melbourne", "AU-QLD, US-WA; AU-VIC-MELBOURNE")
        assert custom.geo_codes == ["AU-QLD", "US-WA", "AU-VIC-MELBOURNE"]
        set_active(session, custom.id)
        assert active_scope(session).name == "QLD+WA+Melbourne"

        au_source = SourceDefinition(
            name="AU register",
            source_category="official_register",
            access_method="api",
            geo_codes=["AU"],
            authority_tier=1,
        )
        nz_source = SourceDefinition(
            name="NZ register",
            source_category="official_register",
            access_method="api",
            geo_codes=["NZ"],
            authority_tier=1,
        )
        assert source_in_scope(au_source, active_scope(session))  # AU covers AU-QLD
        assert not source_in_scope(nz_source, active_scope(session))  # NZ not in custom scope


# --- Raw store path safety --------------------------------------------------------


def test_rawstore_rejects_path_escape(settings):
    settings.ensure_data_dirs()
    with pytest.raises(FileNotFoundError):
        rawstore.read_bytes(settings, "../../heatseeker.db")
