"""Robust source identity, contextual coverage, lineage, and migration invariants."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from heatseeker_api import ui_sources
from heatseeker_api.main import create_app
from heatseeker_common.db import create_db_engine, session_scope
from heatseeker_common.migrate import build_alembic_config
from heatseeker_common.models import AuditLog
from heatseeker_common.settings import Settings
from heatseeker_industry_packs.loader import load_pack
from heatseeker_source_registry.collect import collect_source
from heatseeker_source_registry.identity import canonicalise_url
from heatseeker_source_registry.models import (
    ResearchScope,
    RobotsStatus,
    SourceCoverage,
    SourceCoverageTarget,
    SourceDefinition,
    SourceDocument,
    SourceIdentity,
    SourceLifecycle,
    TermsStatus,
)
from heatseeker_source_registry.policy import check_coverage_robots
from heatseeker_source_registry.scopes import create_scope, source_in_scope
from heatseeker_source_registry.sync import sync_pack_seeds
from heatseeker_source_registry.targeting import (
    CoverageSpec,
    CoverageValidationError,
    TargetSpec,
    match_coverages,
    upsert_coverage,
)
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError


def _source(**overrides) -> SourceDefinition:
    values = {
        "name": "Shared intelligence feed",
        "source_category": "news",
        "base_url": "https://shared.example/feed",
        "access_method": "rss",
        "authority_tier": 4,
        "lifecycle_status": SourceLifecycle.ACTIVE,
        "robots_status": RobotsStatus.ALLOWED,
        "terms_status": TermsStatus.APPROVED,
    }
    values.update(overrides)
    return SourceDefinition(**values)


def _coverage(key: str, industry: str, region: str, *extra: TargetSpec) -> CoverageSpec:
    return CoverageSpec(
        coverage_key=key,
        targets=(
            TargetSpec("industry", industry),
            TargetSpec("region", region),
            *extra,
        ),
    )


def test_coverage_tuples_prevent_cartesian_leaks():
    coverages = [
        _coverage("scaffolding_au", "scaffolding", "AU"),
        _coverage("coffee_nz", "coffee_roasting", "NZ"),
    ]
    assert match_coverages(coverages, industry_ids=["scaffolding"], region_codes=["AU-QLD"])
    assert match_coverages(coverages, industry_ids=["coffee_roasting"], region_codes=["NZ-AUK"])
    assert not match_coverages(coverages, industry_ids=["scaffolding"], region_codes=["NZ"])
    assert not match_coverages(coverages, industry_ids=["coffee_roasting"], region_codes=["AU"])


def test_global_unknown_and_exclusions_are_distinct():
    unknown_region = CoverageSpec(
        coverage_key="industry_only",
        targets=(TargetSpec("industry", "scaffolding"),),
    )
    assert not match_coverages([unknown_region], industry_ids=["scaffolding"], region_codes=["AU"])
    assert match_coverages(
        [unknown_region],
        industry_ids=["scaffolding"],
        region_codes=["AU"],
        include_unknown=True,
    )

    global_coverage = _coverage("global", "scaffolding", "GLOBAL")
    assert match_coverages([global_coverage], industry_ids=["scaffolding"], region_codes=["US-TX"])

    australia_except_qld = _coverage(
        "au_except_qld",
        "scaffolding",
        "AU",
        TargetSpec("region", "AU-QLD", polarity="exclude", match_mode="hierarchical"),
    )
    assert not match_coverages(
        [australia_except_qld],
        industry_ids=["scaffolding"],
        region_codes=["AU-QLD-BRISBANE"],
    )
    assert match_coverages(
        [australia_except_qld],
        industry_ids=["scaffolding"],
        region_codes=["AU-VIC"],
    )

    covers = CoverageSpec(
        coverage_key="covers_au",
        targets=(
            TargetSpec("industry", "scaffolding"),
            TargetSpec("region", "AU", match_mode="covers"),
        ),
    )
    assert match_coverages([covers], industry_ids=["scaffolding"], region_codes=["AU-QLD"])
    assert not match_coverages([covers], industry_ids=["scaffolding"], region_codes=["ANZ"])
    within = CoverageSpec(
        coverage_key="within_au",
        targets=(
            TargetSpec("industry", "scaffolding"),
            TargetSpec("region", "AU-QLD", match_mode="within"),
        ),
    )
    assert match_coverages([within], industry_ids=["scaffolding"], region_codes=["AU"])


def test_source_url_identity_is_conservative_and_stable():
    assert (
        canonicalise_url("HTTPS://Example.COM:443/feed/?z=2&a=1#fragment")
        == "https://example.com/feed?a=1&z=2"
    )
    assert canonicalise_url("http://example.com:80/") == "http://example.com/"
    with pytest.raises(ValueError, match="credentials"):
        canonicalise_url("https://user:password@example.com/")


def test_coverage_validation_rejects_ambiguous_targets():
    with pytest.raises(CoverageValidationError, match="both included and excluded"):
        CoverageSpec(
            coverage_key="bad",
            targets=(
                TargetSpec("industry", "scaffolding"),
                TargetSpec("industry", "scaffolding", polarity="exclude"),
            ),
        ).normalised()
    with pytest.raises(CoverageValidationError, match="invalid geography code"):
        TargetSpec("region", "somewhere!!").normalised()
    with pytest.raises(CoverageValidationError, match="only supported for 'region'"):
        TargetSpec("industry", "scaffolding", match_mode="hierarchical").normalised()
    with pytest.raises(CoverageValidationError, match="timezone"):
        replace(
            _coverage("naive", "scaffolding", "AU"),
            valid_from=datetime(2026, 1, 1),
        ).normalised()
    expired = replace(
        _coverage("expired", "scaffolding", "AU"),
        valid_to=datetime(2020, 1, 1, tzinfo=UTC),
    )
    explanation = match_coverages([expired], industry_ids=["scaffolding"], region_codes=["AU"])
    assert not explanation
    assert explanation.coverages[0].reason == "coverage has expired"


def test_database_constraints_and_evidence_delete_guard(engine):
    with session_scope(engine) as session:
        source = _source()
        session.add(source)
        session.flush()
        coverage, _ = upsert_coverage(session, source, _coverage("core", "scaffolding", "AU"))
        session.add(
            SourceIdentity(
                source_definition_id=source.id,
                identity_type="url",
                identity_value=source.base_url,
                normalised_value=source.base_url,
                is_primary=True,
                origin="test",
            )
        )
        document = SourceDocument(
            source_definition_id=source.id,
            source_coverage_id=coverage.id,
            source_url=source.base_url,
            content_hash="a" * 64,
            size_bytes=4,
            raw_storage_path="aa/aa/" + "a" * 64,
            collector_version="test/1",
        )
        session.add(document)
        source_id, coverage_id = source.id, coverage.id

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.delete(session.get(SourceDefinition, source_id))

    with session_scope(engine) as session:
        assert session.get(SourceDefinition, source_id) is not None
        assert session.query(SourceDocument).count() == 1

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            SourceIdentity(
                source_definition_id=source_id,
                identity_type="source_key",
                identity_value="another_primary",
                normalised_value="another_primary",
                is_primary=True,
                origin="test",
            )
        )

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            SourceCoverageTarget(
                source_coverage_id=coverage_id,
                dimension="industry",
                target_key="scaffolding",
                polarity="exclude",
                match_mode="exact",
            )
        )

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add_all(
            [
                ResearchScope(name="First active", geo_codes=["AU"], is_active=True),
                ResearchScope(name="Second active", geo_codes=["NZ"], is_active=True),
            ]
        )

    with session_scope(engine) as session:
        first_source = _source(name="First")
        second_source = _source(name="Second", base_url="https://second.example/")
        session.add_all([first_source, second_source])
        session.flush()
        second_coverage, _ = upsert_coverage(
            session, second_source, _coverage("second", "coffee", "NZ")
        )
        first_source_id = first_source.id
        second_coverage_id = second_coverage.id
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            SourceDocument(
                source_definition_id=first_source_id,
                source_coverage_id=second_coverage_id,
                source_url="https://shared.example/mismatched",
                content_hash="c" * 64,
                size_bytes=1,
                raw_storage_path="cc/cc/hash",
                collector_version="test/1",
            )
        )


def test_v1_sync_is_idempotent_and_backfills_context(engine):
    from heatseeker_industry_packs.loader import default_packs_root

    pack = load_pack(default_packs_root() / "scaffolding_anz")
    with session_scope(engine) as session:
        first = sync_pack_seeds(session, pack, actor="test")
        assert first["sources_created"] >= 20
        source = session.scalars(
            select(SourceDefinition).where(SourceDefinition.name.contains("Business Register"))
        ).first()
        source_id = source.id
        source_updated_at = source.updated_at
        coverage_updated_at = source.coverages[0].updated_at
        audit_count = session.query(AuditLog).count()

    with session_scope(engine) as session:
        second = sync_pack_seeds(session, pack, actor="test")
        assert second["sources_created"] == 0
        assert second["sources_updated"] == 0
        assert second["coverages_updated"] == 0
        assert second["coverages_unchanged"] >= 20
        source = session.get(SourceDefinition, source_id)
        assert source.updated_at == source_updated_at
        assert source.coverages[0].updated_at == coverage_updated_at
        assert session.query(AuditLog).count() == audit_count
        dimensions = {
            (target.dimension, target.target_key) for target in source.coverages[0].targets
        }
        assert ("industry", "scaffolding_anz") in dimensions
        assert ("region", "AU") in dimensions


def _write_v2_pack(path, *, pack_id: str, industry: str, region: str) -> None:
    path.mkdir(parents=True)
    (path / "sources").mkdir()
    (path / "manifest.yaml").write_text(
        f"schema: pack_manifest/v1\nid: {pack_id}\nname: {pack_id}\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (path / "sources" / "seed_sources.yaml").write_text(
        "schema: seed_sources/v2\n"
        f"pack: {pack_id}\n"
        "sources:\n"
        "  - key: shared_feed\n"
        "    source_key: shared_global_feed\n"
        f"    name: Shared Feed from {pack_id}\n"
        "    category: news\n"
        "    url: https://shared.example/feed\n"
        "    access: rss\n"
        "    authority_tier: 4\n"
        "    language: en\n"
        "    expected_update_frequency: PT1H\n"
        "    authentication_type: api_key\n"
        "    rate_limit_policy: {requests_per_minute: 10}\n"
        "    coverages:\n"
        "      - key: core\n"
        f"        industry_ids: [{industry}]\n"
        f"        region_codes: [{region}]\n"
        "        include_targets: {purpose: [company_discovery]}\n",
        encoding="utf-8",
    )


def test_v2_shared_source_has_two_non_cartesian_pack_coverages(engine, tmp_path):
    first_path = tmp_path / "scaffolding_pack"
    second_path = tmp_path / "coffee_pack"
    _write_v2_pack(first_path, pack_id="scaffolding_pack", industry="scaffolding", region="AU")
    _write_v2_pack(second_path, pack_id="coffee_pack", industry="coffee_roasting", region="NZ")
    first, second = load_pack(first_path), load_pack(second_path)
    with session_scope(engine) as session:
        sync_pack_seeds(session, first, actor="test")
        sync_pack_seeds(session, second, actor="test")

    with session_scope(engine) as session:
        assert session.query(SourceDefinition).count() == 1
        source = session.query(SourceDefinition).one()
        assert source.name == "Shared Feed from scaffolding_pack"  # no last writer wins
        assert len(source.coverages) == 2
        assert len(source.identities) == 4  # URL + shared key + two pack seed aliases
        assert match_coverages(source.coverages, industry_ids=["scaffolding"], region_codes=["AU"])
        assert match_coverages(
            source.coverages, industry_ids=["coffee_roasting"], region_codes=["NZ"]
        )
        assert not match_coverages(
            source.coverages, industry_ids=["scaffolding"], region_codes=["NZ"]
        )


def test_removed_pack_seed_disables_coverage_without_deleting_source(engine, tmp_path):
    pack_path = tmp_path / "removal_pack"
    _write_v2_pack(pack_path, pack_id="removal_pack", industry="scaffolding", region="AU")
    with session_scope(engine) as session:
        sync_pack_seeds(session, load_pack(pack_path), actor="test")

    seed_path = pack_path / "sources" / "seed_sources.yaml"
    seed_path.write_text(
        "schema: seed_sources/v2\npack: removal_pack\nsources: []\n",
        encoding="utf-8",
    )
    with session_scope(engine) as session:
        result = sync_pack_seeds(session, load_pack(pack_path), actor="test")
        assert result["coverages_disabled"] == 1
    with session_scope(engine) as session:
        assert session.query(SourceDefinition).count() == 1
        assert session.query(SourceCoverage).one().lifecycle_status == "disabled"


def test_seed_identity_conflict_is_atomic(engine, tmp_path):
    pack_path = tmp_path / "conflict_pack"
    _write_v2_pack(pack_path, pack_id="conflict_pack", industry="scaffolding", region="AU")
    with session_scope(engine) as session:
        by_pack = _source(name="Pack identity owner", base_url="https://pack.example/")
        by_url = _source(name="URL identity owner")
        session.add_all([by_pack, by_url])
        session.flush()
        session.add_all(
            [
                SourceIdentity(
                    source_definition_id=by_pack.id,
                    identity_type="pack_seed",
                    identity_value="conflict_pack:shared_feed",
                    normalised_value="conflict_pack:shared_feed",
                    is_primary=True,
                    origin="test",
                ),
                SourceIdentity(
                    source_definition_id=by_url.id,
                    identity_type="url",
                    identity_value="https://shared.example/feed",
                    normalised_value="https://shared.example/feed",
                    is_primary=True,
                    origin="test",
                ),
            ]
        )
    with session_scope(engine) as session:
        result = sync_pack_seeds(session, load_pack(pack_path), actor="test")
        assert len(result["conflicts"]) == 1
        assert result["total"] == 1
        assert result["processed"] == 0
    with session_scope(engine) as session:
        assert session.query(SourceDefinition).count() == 2
        assert session.query(SourceCoverage).count() == 0


def test_collection_snapshots_and_merges_coverage_context(engine, settings):
    with session_scope(engine) as session:
        source = _source()
        session.add(source)
        session.flush()
        first, _ = upsert_coverage(
            session, source, _coverage("scaffolding_au", "scaffolding", "AU")
        )
        second, _ = upsert_coverage(
            session, source, _coverage("coffee_au", "coffee_roasting", "AU")
        )
        source_id, first_id, second_id = source.id, first.id, second.id

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"same contextual document")
    )
    with session_scope(engine) as session:
        result = collect_source(
            session,
            settings,
            source_id,
            transport=transport,
            coverage_id=first_id,
            scope_snapshot={"id": "scope-au", "industry_ids": ["scaffolding"]},
        )
        assert result["outcome"] == "collected"
    with session_scope(engine) as session:
        result = collect_source(
            session,
            settings,
            source_id,
            transport=transport,
            coverage_id=second_id,
            scope_snapshot={"id": "scope-coffee", "industry_ids": ["coffee_roasting"]},
        )
        assert result["outcome"] == "duplicate"
    with session_scope(engine) as session:
        document = session.query(SourceDocument).one()
        assert document.source_coverage_id == first_id
        assert set(document.targeting_snapshot["coverage_ids"]) == {first_id, second_id}
        assert {scope["id"] for scope in document.targeting_snapshot["research_scopes"]} == {
            "scope-au",
            "scope-coffee",
        }
        assert document.access_policy_snapshot["source_coverage_id"] == first_id


def test_same_content_at_distinct_urls_remains_distinct_evidence(engine, settings):
    with session_scope(engine) as session:
        source = _source()
        session.add(source)
        session.flush()
        first, _ = upsert_coverage(session, source, _coverage("primary", "scaffolding", "AU"))
        second_spec = _coverage("alternate", "scaffolding", "NZ")
        second_spec = replace(
            second_spec,
            collection_scope_override={"endpoint_url": "https://shared.example/alternate"},
        )
        second, _ = upsert_coverage(session, source, second_spec)
        second.robots_status = RobotsStatus.ALLOWED
        source_id, first_id, second_id = source.id, first.id, second.id
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"same"))
    with session_scope(engine) as session:
        collect_source(session, settings, source_id, transport=transport, coverage_id=first_id)
    with session_scope(engine) as session:
        collect_source(session, settings, source_id, transport=transport, coverage_id=second_id)
    with session_scope(engine) as session:
        assert session.query(SourceDocument).count() == 2


def test_distinct_coverage_endpoint_requires_its_own_robots_check(engine, settings):
    settings.robots_policy = "enforce"
    with session_scope(engine) as session:
        source = _source()
        session.add(source)
        session.flush()
        coverage, _ = upsert_coverage(
            session,
            source,
            CoverageSpec(
                coverage_key="alternate_endpoint",
                collection_scope_override={"endpoint_url": "https://other.example/private/feed"},
                targets=(TargetSpec("region", "AU"),),
            ),
        )
        source_id, coverage_id = source.id, coverage.id

    with session_scope(engine) as session:
        blocked = collect_source(session, settings, source_id, coverage_id=coverage_id)
        assert blocked["outcome"] == "blocked"
        assert "coverage endpoint" in blocked["error"]

    disallow = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text="User-agent: *\nDisallow: /private\n"
            if request.url.path == "/robots.txt"
            else "content",
        )
    )
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        coverage = session.get(SourceCoverage, coverage_id)
        assert (
            check_coverage_robots(settings, source, coverage, transport=disallow)
            == RobotsStatus.DISALLOWED
        )
        assert source.robots_status == RobotsStatus.ALLOWED  # independent policy state


def test_multidimensional_research_scope_uses_one_coverage(engine):
    with session_scope(engine) as session:
        source = _source()
        session.add(source)
        session.flush()
        upsert_coverage(session, source, _coverage("scaffolding_au", "scaffolding", "AU"))
        upsert_coverage(session, source, _coverage("coffee_nz", "coffee", "NZ"))
        matching = create_scope(
            session,
            "Scaffolding AU",
            "AU-QLD",
            industry_ids_raw="scaffolding",
            include_unknown=False,
        )
        leaking = create_scope(
            session,
            "Scaffolding NZ",
            "NZ",
            industry_ids_raw="scaffolding",
            include_unknown=False,
        )
        assert source_in_scope(source, matching)
        assert not source_in_scope(source, leaking)


def test_source_api_crud_filters_and_relationships(engine, settings):
    client = TestClient(create_app(settings))
    secret_rejected = client.post(
        "/api/sources",
        json={
            "name": "Unsafe config",
            "source_category": "news",
            "base_url": "https://unsafe.example/",
            "access_method": "html",
            "collection_scope": {"api_key": "do-not-store-this"},
        },
    )
    assert secret_rejected.status_code == 422
    created = client.post(
        "/api/sources",
        json={
            "name": "Manual shared feed",
            "source_category": "news",
            "base_url": "https://manual.example/feed",
            "access_method": "rss",
            "authority_tier": 4,
            "language": "en",
        },
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    bad_coverage = client.post(
        f"/api/sources/{source_id}/coverages",
        json={
            "coverage_key": "unsafe",
            "collection_scope_override": {"token": "also-unsafe"},
        },
    )
    assert bad_coverage.status_code == 400
    patched_source = client.patch(
        f"/api/sources/{source_id}",
        json={
            "base_url": "https://manual.example/v2/feed",
            "expected_update_frequency": "PT30M",
        },
    )
    assert patched_source.status_code == 200
    assert patched_source.json()["expected_update_frequency"] == "PT30M"

    coverage_ids = []
    for key, industry, region in (
        ("scaffolding_au", "scaffolding", "AU"),
        ("coffee_nz", "coffee", "NZ"),
    ):
        response = client.post(
            f"/api/sources/{source_id}/coverages",
            json={
                "coverage_key": key,
                "industry_ids": [industry],
                "region_codes": [region],
                "targets": [{"dimension": "purpose", "target_key": "company_discovery"}],
            },
        )
        assert response.status_code == 201, response.text
        coverage_ids.append(response.json()["id"])

    matching = client.get(
        "/api/sources",
        params={"industry_id": "scaffolding", "region_code": "AU-QLD", "include": "pairings"},
    )
    assert [row["id"] for row in matching.json()] == [source_id]
    leaked = client.get(
        "/api/sources",
        params={"industry_id": "scaffolding", "region_code": "NZ"},
    )
    assert leaked.json() == []
    summary = client.get("/api/source-coverages/summary").json()
    cells = {(cell["industry_id"], cell["region_code"]) for cell in summary["matrix"]}
    assert cells == {("scaffolding", "AU"), ("coffee", "NZ")}
    resolved = client.get(
        "/api/sources/resolve",
        params={"industry_id": "coffee", "region_code": "NZ", "status": "candidate"},
    )
    assert resolved.status_code == 200
    assert resolved.json()[0]["match"]["matched"] is True

    other = client.post(
        "/api/sources",
        json={
            "name": "Original publisher",
            "source_category": "news",
            "base_url": "https://publisher.example/",
            "access_method": "html",
        },
    ).json()
    relationship = client.post(
        f"/api/sources/{source_id}/relationships",
        json={
            "related_source_definition_id": other["id"],
            "relationship_type": "syndicates_from",
            "confidence": 0.9,
        },
    )
    assert relationship.status_code == 201
    detail = client.get(f"/api/sources/{source_id}").json()
    assert detail["identities"][0]["type"] == "url"
    assert detail["relationships"][0]["relationship_type"] == "syndicates_from"

    patched = client.patch(f"/api/source-coverages/{coverage_ids[0]}", json={"priority": 88})
    assert patched.status_code == 200
    assert patched.json()["priority"] == 88
    disabled = client.delete(f"/api/source-coverages/{coverage_ids[0]}")
    assert disabled.status_code == 200
    assert disabled.json()["lifecycle_status"] == "disabled"

    scope = client.post(
        "/api/scopes",
        json={
            "name": "Coffee NZ",
            "industry_ids": ["coffee"],
            "geo_codes": ["NZ"],
            "target_filters": {"purpose": ["company_discovery"]},
            "include_unknown": False,
        },
    )
    assert scope.status_code == 201, scope.text
    activated = client.post(f"/api/scopes/{scope.json()['id']}/activate")
    assert activated.status_code == 200
    assert client.get("/api/scopes/active").json()["name"] == "Coffee NZ"
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        source.lifecycle_status = SourceLifecycle.ACTIVE
        source.robots_status = RobotsStatus.ALLOWED
        source.terms_status = TermsStatus.APPROVED
    queued = client.post(
        f"/api/sources/{source_id}/collect",
        json={"coverage_id": coverage_ids[1], "scope_id": scope.json()["id"]},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["payload"]["scope_snapshot"]["name"] == "Coffee NZ"


def test_source_and_coverage_workflow_via_ui(engine, settings):
    client = TestClient(create_app(settings))
    response = client.post(
        "/sources/create",
        data={
            "name": "Manual policy bulletin",
            "source_category": "regulator",
            "base_url": "https://bulletin.example/",
            "access_method": "html",
            "authority_tier": "2",
            "geo_codes": "AU",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope(engine) as session:
        source = session.query(SourceDefinition).one()
        source_id = source.id

    paired = client.post(
        f"/sources/{source_id}/coverages/create",
        data={
            "coverage_key": "regulatory_au",
            "name": "Australian regulation",
            "industry_ids": "scaffolding",
            "region_codes": "AU",
            "dimension": "purpose",
            "target_key": "regulatory_monitoring",
            "priority": "80",
            "relevance": "0.9",
            "confidence": "0.8",
        },
        follow_redirects=False,
    )
    assert paired.status_code == 303
    page = client.get(f"/sources/{source_id}")
    assert page.status_code == 200
    assert "regulatory_au" in page.text
    assert "industry:scaffolding" in page.text
    assert "purpose:regulatory_monitoring" in page.text

    edited = client.post(
        f"/sources/{source_id}/edit",
        data={
            "name": "Edited policy bulletin",
            "source_category": "regulator",
            "base_url": "https://bulletin.example/v2",
            "access_method": "html",
            "authority_tier": "2",
            "geo_codes": "AU",
            "language": "en",
            "expected_update_frequency": "P1D",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert "Edited policy bulletin" in client.get(f"/sources/{source_id}").text

    filtered = client.get(
        "/sources", params={"industry_id": "scaffolding", "region_code": "AU-QLD"}
    )
    assert "Edited policy bulletin" in filtered.text


def test_robots_override_controls_api_activation_and_ui(engine, settings):
    settings.robots_policy = "ignore"
    with session_scope(engine) as session:
        source = _source(
            name="Robots advisory source",
            lifecycle_status=SourceLifecycle.CANDIDATE,
            robots_status=RobotsStatus.DISALLOWED,
        )
        session.add(source)
        session.flush()
        source_id = source.id

    client = TestClient(create_app(settings))
    activated = client.post(f"/api/sources/{source_id}/activate")
    assert activated.status_code == 200
    assert activated.json()["lifecycle_status"] == SourceLifecycle.ACTIVE

    changed = client.post(
        f"/sources/{source_id}/robots-policy",
        data={"robots_policy": "enforce"},
        follow_redirects=False,
    )
    assert changed.status_code == 303
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        assert source.respect_robots_override is True
        source.lifecycle_status = SourceLifecycle.CANDIDATE

    blocked = client.post(f"/api/sources/{source_id}/activate")
    assert blocked.status_code == 409
    assert "robots" in blocked.json()["detail"]
    detail = client.get(f"/sources/{source_id}")
    assert "always enforce" in detail.text
    assert "Effective mode: <strong>enforce</strong>" in detail.text


def test_ui_collect_uses_fresh_transaction_for_enqueue(engine, settings, monkeypatch):
    """A worker commit between validation and enqueue must not stale the write."""
    with session_scope(engine) as session:
        source = _source(name="Concurrent collection source")
        session.add(source)
        session.flush()
        source_id = source.id

    original_enqueue = ui_sources.jobs.enqueue

    def enqueue_after_concurrent_commit(session, *args, **kwargs):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE source_definition SET updated_at = updated_at WHERE id = :id"),
                {"id": source_id},
            )
        return original_enqueue(session, *args, **kwargs)

    monkeypatch.setattr(ui_sources.jobs, "enqueue", enqueue_after_concurrent_commit)
    client = TestClient(create_app(settings))
    response = client.post(f"/sources/{source_id}/collect", follow_redirects=False)

    assert response.status_code == 303
    with session_scope(engine) as session:
        from heatseeker_common.models import Job

        queued = session.scalars(select(Job).where(Job.job_type == "sources.collect")).one()
        assert queued.payload["source_id"] == source_id


def test_legacy_0003_database_upgrades_without_losing_ids_or_documents(tmp_path):
    settings = Settings(data_dir=tmp_path / "legacy", _env_file=None)
    settings.ensure_data_dirs()
    config = build_alembic_config(settings)
    command.upgrade(config, "0003")
    legacy_engine = create_db_engine(settings)
    now = datetime.now(UTC).replace(tzinfo=None)
    source_id = "11111111-1111-1111-1111-111111111111"
    document_id = "22222222-2222-2222-2222-222222222222"
    with legacy_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_definition "
                "(id,name,source_category,base_url,jurisdiction,geo_codes,access_method,"
                "authority_tier,lifecycle_status,robots_status,terms_status,origin,pack_id,"
                "consecutive_failures,created_at,updated_at) VALUES "
                "(:id,'Legacy source','news','https://legacy.example/','AU',:geo,'html',4,"
                "'candidate','unknown','unreviewed','pack_seed','legacy_pack',0,:now,:now)"
            ),
            {"id": source_id, "geo": json.dumps(["au", "AU"]), "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO source_document "
                "(id,source_definition_id,source_url,retrieved_at,first_seen_at,last_seen_at,"
                "retrieval_count,content_hash,size_bytes,raw_storage_path,collector_version) "
                "VALUES (:id,:source_id,'https://legacy.example/',:now,:now,:now,1,:hash,4,"
                "'aa/aa/hash','legacy/1')"
            ),
            {"id": document_id, "source_id": source_id, "now": now, "hash": "b" * 64},
        )
    legacy_engine.dispose()

    command.upgrade(config, "head")
    engine = create_db_engine(settings)
    with session_scope(engine) as session:
        source = session.get(SourceDefinition, source_id)
        assert source is not None
        assert session.get(SourceDocument, document_id) is not None
        assert len(source.coverages) == 1
        assert {
            (target.dimension, target.target_key) for target in source.coverages[0].targets
        } == {
            ("industry", "legacy_pack"),
            ("region", "AU"),
        }
    engine.dispose()

    command.downgrade(config, "0003")
    engine = create_db_engine(settings)
    columns = {column["name"] for column in inspect(engine).get_columns("source_document")}
    assert "source_coverage_id" not in columns
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM source_document WHERE id=:id"), {"id": document_id}
            ).scalar_one()
            == 1
        )
    engine.dispose()
