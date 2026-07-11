"""Named regions as data (ADR-0012): seeding, loading, editing, deletion guards."""

import pytest
from heatseeker_common.db import session_scope
from heatseeker_core_domain.geography import describe, match_geography, validate_code
from heatseeker_source_registry.models import GeoRegion, ResearchScope
from heatseeker_source_registry.regions import (
    delete_region,
    ensure_default_regions,
    load_regions,
    upsert_region,
)
from sqlalchemy import select


def test_seeding_is_idempotent_and_additive(engine):
    with session_scope(engine) as session:
        ensure_default_regions(session)
        first = set(session.scalars(select(GeoRegion.code)))
        assert {"ANZ", "APAC", "LATAM", "MIDDLE_EAST", "AFRICA"} <= first
        assert "GLOBAL" not in first  # special-cased in matching, not a row
        ensure_default_regions(session)
        assert set(session.scalars(select(GeoRegion.code))) == first  # no duplicates

        # A user edit survives re-seeding (never overwritten).
        apac = session.scalars(select(GeoRegion).where(GeoRegion.code == "APAC")).one()
        apac.member_codes = ["AU", "NZ"]
        ensure_default_regions(session)
        apac = session.scalars(select(GeoRegion).where(GeoRegion.code == "APAC")).one()
        assert apac.member_codes == ["AU", "NZ"]


def test_custom_region_becomes_matchable_after_upsert(engine):
    with session_scope(engine) as session:
        upsert_region(
            session, "southeast_asia", "Southeast Asia", ["SG", "MY", "TH", "VN"], actor="test"
        )
    assert validate_code("SOUTHEAST_ASIA") == "SOUTHEAST_ASIA"
    assert describe("SOUTHEAST_ASIA") == "Southeast Asia"
    assert match_geography(["VN"], ["SOUTHEAST_ASIA"])
    assert not match_geography(["AU"], ["SOUTHEAST_ASIA"])


def test_editing_a_builtin_changes_matching(engine):
    with session_scope(engine) as session:
        ensure_default_regions(session)
        upsert_region(session, "ANZ", "Australia & New Zealand", ["AU", "NZ", "FJ"])
    assert match_geography(["FJ"], ["ANZ"])  # Fiji now counts as ANZ


def test_builtin_and_referenced_regions_cannot_be_deleted(engine):
    with session_scope(engine) as session:
        ensure_default_regions(session)
        with pytest.raises(ValueError, match="builtin"):
            delete_region(session, "APAC")

        upsert_region(session, "GULF", "Gulf states", ["AE", "SA", "QA"])
        session.add(ResearchScope(name="Gulf scope", geo_codes=["GULF"]))
        session.flush()
        with pytest.raises(ValueError, match="referenced by scope 'Gulf scope'"):
            delete_region(session, "GULF")


def test_deleting_unreferenced_custom_region_works(engine):
    with session_scope(engine) as session:
        upsert_region(session, "GULF", "Gulf states", ["AE", "SA", "QA"])
        delete_region(session, "GULF")
        assert session.scalars(select(GeoRegion).where(GeoRegion.code == "GULF")).first() is None
    with pytest.raises(Exception, match="invalid geography code"):
        validate_code("GULF")  # registry refreshed — code no longer valid


def test_load_regions_replaces_registry_from_db(engine):
    with session_scope(engine) as session:
        load_regions(session)  # seeds + loads
    assert validate_code("LATAM") == "LATAM"
    assert match_geography(["BR"], ["LATAM"])
