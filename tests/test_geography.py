"""Named regions are data: registry-backed definitions, member-aware coverage,
custom regions participate in validation and matching like builtins."""

import pytest
from heatseeker_core_domain.geography import (
    GeographyMatchMode,
    InvalidGeographyCode,
    builtin_macro_regions,
    describe,
    match_geography,
    reset_macro_regions,
    set_macro_regions,
    validate_code,
    validate_region_definition,
)


def test_new_builtin_regions_validate_and_match():
    for code in ("LATAM", "MIDDLE_EAST", "AFRICA"):
        assert validate_code(code) == code
    assert match_geography(["BR"], ["LATAM"])
    assert match_geography(["AE"], ["MIDDLE_EAST"])
    assert match_geography(["ZA-WC"], ["AFRICA"])  # subdivision under a member country
    assert not match_geography(["AU"], ["LATAM"])


def test_expanded_apac_and_europe_membership():
    assert match_geography(["BD"], ["APAC"])  # previously fell outside the set
    assert match_geography(["GR"], ["EUROPE"])
    assert match_geography(["RO-B"], ["EUROPE"])


def test_custom_region_participates_everywhere():
    regions = builtin_macro_regions()
    regions["SOUTHEAST_ASIA"] = {"SG", "MY", "TH", "VN", "PH", "ID", "KH", "LA", "MM", "BN"}
    set_macro_regions(regions, names={"SOUTHEAST_ASIA": "Southeast Asia"})

    assert validate_code("SOUTHEAST_ASIA") == "SOUTHEAST_ASIA"  # input boundaries accept it
    assert describe("SOUTHEAST_ASIA") == "Southeast Asia"
    assert match_geography(["VN"], ["SOUTHEAST_ASIA"])
    assert match_geography(["SG-CENTRAL"], ["SOUTHEAST_ASIA"])
    assert not match_geography(["AU"], ["SOUTHEAST_ASIA"])
    # COVERS: the region covers its members, not bystanders.
    assert match_geography(["SOUTHEAST_ASIA"], ["TH"], mode=GeographyMatchMode.COVERS)
    assert not match_geography(["SOUTHEAST_ASIA"], ["AU"], mode=GeographyMatchMode.COVERS)


def test_reset_restores_builtins():
    set_macro_regions({"ONLY_REGION": {"AU"}})
    with pytest.raises(InvalidGeographyCode):
        validate_code("APAC")  # replaced wholesale — APAC gone
    reset_macro_regions()
    assert validate_code("APAC") == "APAC"


def test_subdivision_members_are_covered_correctly():
    regions = builtin_macro_regions()
    regions["PACIFIC_NORTHWEST"] = {"US-WA", "US-OR", "CA-BC"}
    set_macro_regions(regions)

    assert match_geography(["US-WA-SEATTLE"], ["PACIFIC_NORTHWEST"])
    assert match_geography(["PACIFIC_NORTHWEST"], ["US-OR"], mode=GeographyMatchMode.COVERS)
    # The region does NOT cover all of the US, and the US-country code alone
    # does not cover the region (CA-BC is Canadian).
    assert not match_geography(["PACIFIC_NORTHWEST"], ["US"], mode=GeographyMatchMode.COVERS)
    assert not match_geography(["US"], ["PACIFIC_NORTHWEST"], mode=GeographyMatchMode.COVERS)


def test_global_always_present_and_not_redefinable():
    set_macro_regions({"GLOBAL": {"AU"}, "ANZ": {"AU", "NZ"}})
    assert match_geography(["KZ"], ["GLOBAL"])  # still matches everything
    with pytest.raises(InvalidGeographyCode):
        validate_region_definition("GLOBAL", ["AU"])


def test_region_definition_validation():
    code, members = validate_region_definition("gulf states", ["ae", "SA", "QA", "ae"])
    assert code == "GULF_STATES"
    assert members == ["AE", "QA", "SA"]  # normalised, deduplicated, sorted

    with pytest.raises(InvalidGeographyCode, match="may not nest"):
        validate_region_definition("SUPER_REGION", ["APAC", "AU"])
    with pytest.raises(InvalidGeographyCode, match="invalid region code"):
        validate_region_definition("X", ["AU"])  # too short — collides with nothing useful
    with pytest.raises(InvalidGeographyCode, match="invalid region code"):
        validate_region_definition("MY-REGION", ["AU"])  # hyphens are hierarchy
    with pytest.raises(InvalidGeographyCode, match="at least one member"):
        validate_region_definition("EMPTY_REGION", [])
    with pytest.raises(InvalidGeographyCode, match="invalid region member"):
        validate_region_definition("BAD_MEMBER", ["not a code!"])


def test_combined_scope_of_regions_countries_and_cities():
    combo = ["ANZ", "NORTH_AMERICA", "AU-VIC-MELBOURNE"]
    assert match_geography(["AU"], combo)
    assert match_geography(["US-WA"], combo)
    assert not match_geography(["JP"], combo)
    assert not match_geography(["GB"], combo)
