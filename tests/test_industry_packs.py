"""M1 acceptance: pack loading, validation, versioning, second-industry guard."""

from pathlib import Path

import pytest
from heatseeker_common.db import session_scope
from heatseeker_common.models import AuditLog
from heatseeker_industry_packs.loader import (
    PackValidationError,
    default_packs_root,
    discover_packs,
    load_pack,
)
from heatseeker_industry_packs.models import PackRegistration
from heatseeker_industry_packs.registry import register_pack
from sqlalchemy import select

FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"


# --- Scaffolding pack (the real one) ---------------------------------------


def test_scaffolding_pack_loads_and_validates():
    pack = load_pack(default_packs_root() / "scaffolding_anz")
    assert pack.pack_id == "scaffolding_anz"
    assert pack.version == "0.1.0"
    assert len(pack.content_hash) == 64

    archetypes = pack.files["company_archetypes.yaml"].archetypes
    assert len(archetypes) >= 40  # spec §8.4 seeds
    assert any(a.id == "scaffold_design_consultancy" for a in archetypes)

    services = [s for cat in pack.files["service_taxonomy.yaml"].categories for s in cat.services]
    assert len(services) >= 30
    assert any(s.id == "scaffold_design" for s in services)

    seeds = pack.files["sources/seed_sources.yaml"]
    assert seeds.pack == "scaffolding_anz"
    assert seeds.discovery.ai_expansion_enabled is True
    tiers = {source.authority_tier for source in seeds.sources}
    assert 1 in tiers and 6 in tiers  # official + weak-signal both present


def test_discover_packs_finds_scaffolding():
    paths = discover_packs()
    assert any(path.name == "scaffolding_anz" for path in paths)


# --- Second industry: core must not care (spec §41.18) ----------------------


def test_second_industry_pack_loads_without_core_changes(engine):
    pack = load_pack(FIXTURE_PACKS / "coffee_roasting")
    assert pack.pack_id == "coffee_roasting"

    # Registers into the same generic tables — no scaffolding assumptions anywhere.
    with session_scope(engine) as session:
        registration = register_pack(session, pack, actor="test")
        assert registration.pack_id == "coffee_roasting"


# --- Validation catches invalid configuration (M1 acceptance) ---------------


def test_invalid_pack_reports_all_problems():
    with pytest.raises(PackValidationError) as excinfo:
        load_pack(FIXTURE_PACKS / "bad_pack")
    problems = "\n".join(excinfo.value.problems)
    assert "version must be semver" in problems
    assert "must match directory name" in problems
    assert "snake_case" in problems
    assert "unknown pack file 'mystery_file.yaml'" in problems


def test_duplicate_archetype_ids_rejected(tmp_path):
    pack_dir = tmp_path / "dupes"
    pack_dir.mkdir()
    (pack_dir / "manifest.yaml").write_text(
        "schema: pack_manifest/v1\nid: dupes\nname: Dupes\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    (pack_dir / "company_archetypes.yaml").write_text(
        "schema: company_archetypes/v1\n"
        "archetypes:\n"
        "  - {id: same_id, name: First}\n"
        "  - {id: same_id, name: Second}\n",
        encoding="utf-8",
    )
    with pytest.raises(PackValidationError, match="duplicate archetype id"):
        load_pack(pack_dir)


def test_missing_pack_directory_fails():
    with pytest.raises(PackValidationError, match="does not exist"):
        load_pack(FIXTURE_PACKS / "no_such_pack")


def test_missing_manifest_fails(tmp_path):
    pack_dir = tmp_path / "manifestless"
    pack_dir.mkdir()
    (pack_dir / "company_archetypes.yaml").write_text(
        "schema: company_archetypes/v1\narchetypes: []\n", encoding="utf-8"
    )
    with pytest.raises(PackValidationError, match="missing required file"):
        load_pack(pack_dir)


# --- Versioning and registration (M1 acceptance) ----------------------------


def test_registration_records_version_and_hash(engine):
    pack = load_pack(default_packs_root() / "scaffolding_anz")
    with session_scope(engine) as session:
        register_pack(session, pack, actor="test")
    with session_scope(engine) as session:
        row = session.get(PackRegistration, "scaffolding_anz")
        assert row.version == pack.version
        assert row.content_hash == pack.content_hash
        actions = session.scalars(select(AuditLog.action)).all()
        assert "pack.loaded" in actions


def test_reload_same_version_is_idempotent_and_change_is_audited(engine, tmp_path):
    import shutil

    editable = tmp_path / "coffee_roasting"
    shutil.copytree(FIXTURE_PACKS / "coffee_roasting", editable)

    with session_scope(engine) as session:
        register_pack(session, load_pack(editable), actor="test")
        register_pack(session, load_pack(editable), actor="test")  # no-op reload

    # Edit content + bump version -> content hash changes, update audited.
    manifest = editable / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("0.1.0", "0.2.0"), encoding="utf-8"
    )
    changed = load_pack(editable)

    with session_scope(engine) as session:
        registration = register_pack(session, changed, actor="test")
        assert registration.version == "0.2.0"
    with session_scope(engine) as session:
        actions = session.scalars(select(AuditLog.action)).all()
        assert actions.count("pack.loaded") == 1
        assert actions.count("pack.updated") == 1
