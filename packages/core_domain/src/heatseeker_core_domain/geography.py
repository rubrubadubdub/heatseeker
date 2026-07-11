"""Generic geography codes and scope matching (spec §6.5: geography is a core concept).

Code forms (hierarchical, prefix-based):
  Named region:  APAC, ANZ, LATAM, MIDDLE_EAST, ... — a registered set of codes; GLOBAL
                 is special-cased to match everything
  Country:       ISO 3166-1 alpha-2 (AU, NZ, US, GB, ...)
  Subdivision:   ISO 3166-2 (AU-QLD, US-WA, AU-VIC, ...)
  Locality:      subdivision + slug (AU-VIC-MELBOURNE) or country + slug (NZ-AUCKLAND)

Matching rule: a thing is *in scope* when its code is an ancestor OR descendant of any
scope code. A national source (AU) covers Queensland research (AU-QLD); a Queensland
source is relevant to an Australia-wide scope. GLOBAL matches everything.

Directional callers can choose exact, covers, or within instead of symmetric overlap;
named-region coverage is set-aware (AU does not claim to cover all of ANZ).

Named regions are **data, not code**: the module ships builtin defaults (below), and the
application layer replaces the working registry from the database via
``set_macro_regions`` (see heatseeker_source_registry.regions). Region members may be
countries or hierarchical codes (a "PACIFIC_NORTHWEST" of US-WA, US-OR, CA-BC is valid);
regions may not nest.
"""

import enum
import re
from collections.abc import Iterable, Mapping

# Builtin named-region defaults. These seed the database on first boot and are the
# fallback when no database-backed registry has been loaded (e.g. pure unit tests).
# They are starting points, not gazetteers — users edit the live registry in the GUI.
_BUILTIN_MACRO_REGIONS: dict[str, frozenset[str]] = {
    "GLOBAL": frozenset(),  # special-cased: matches everything
    "ANZ": frozenset({"AU", "NZ"}),
    "APAC": frozenset(
        {
            "AU",
            "NZ",
            "JP",
            "KR",
            "CN",
            "TW",
            "HK",
            "SG",
            "MY",
            "TH",
            "VN",
            "PH",
            "ID",
            "IN",
            "PG",
            "FJ",
            "BD",
            "LK",
            "MM",
            "KH",
            "LA",
            "BN",
            "NP",
            "PK",
            "MO",
            "TL",
        }
    ),
    "NORTH_AMERICA": frozenset({"US", "CA", "MX"}),
    "LATAM": frozenset(
        {
            "MX",
            "BR",
            "AR",
            "CL",
            "CO",
            "PE",
            "EC",
            "UY",
            "PY",
            "BO",
            "VE",
            "CR",
            "PA",
            "GT",
            "HN",
            "SV",
            "NI",
            "DO",
            "CU",
        }
    ),
    "EUROPE": frozenset(
        {
            "GB",
            "IE",
            "FR",
            "DE",
            "NL",
            "BE",
            "ES",
            "PT",
            "IT",
            "CH",
            "AT",
            "DK",
            "SE",
            "NO",
            "FI",
            "PL",
            "CZ",
            "GR",
            "RO",
            "HU",
            "SK",
            "SI",
            "HR",
            "BG",
            "EE",
            "LV",
            "LT",
            "LU",
            "IS",
            "MT",
            "CY",
        }
    ),
    "MIDDLE_EAST": frozenset(
        {
            "AE",
            "SA",
            "QA",
            "KW",
            "BH",
            "OM",
            "IL",
            "JO",
            "LB",
            "IQ",
            "IR",
            "TR",
            "YE",
        }
    ),
    "AFRICA": frozenset(
        {
            "ZA",
            "NG",
            "KE",
            "EG",
            "MA",
            "TN",
            "DZ",
            "GH",
            "ET",
            "TZ",
            "UG",
            "ZM",
            "ZW",
            "BW",
            "NA",
            "MZ",
            "SN",
            "CI",
            "CM",
            "RW",
        }
    ),
}

# Working registry consulted by all matching/validation below. Replaced wholesale by
# set_macro_regions() when the database-backed registry loads; defaults to builtins.
MACRO_REGIONS: dict[str, set[str]] = {
    code: set(members) for code, members in _BUILTIN_MACRO_REGIONS.items()
}

# Display names for database-defined regions (overlays KNOWN_CODES in describe()).
_REGION_NAMES: dict[str, str] = {}


def builtin_macro_regions() -> dict[str, set[str]]:
    """Copy of the shipped defaults, for seeding the database-backed registry."""
    return {code: set(members) for code, members in _BUILTIN_MACRO_REGIONS.items()}


def set_macro_regions(
    regions: Mapping[str, Iterable[str]], names: Mapping[str, str] | None = None
) -> None:
    """Replace the working named-region registry (called by the DB-backed loader).

    GLOBAL is always present and always empty — it matches by special case, not by
    membership, and must not be redefined into something narrower.
    """
    MACRO_REGIONS.clear()
    MACRO_REGIONS["GLOBAL"] = set()
    for code, members in regions.items():
        normalised = normalise_code(code)
        if normalised == "GLOBAL":
            continue
        MACRO_REGIONS[normalised] = {normalise_code(m) for m in members}
    _REGION_NAMES.clear()
    if names:
        _REGION_NAMES.update({normalise_code(code): name for code, name in names.items()})


def reset_macro_regions() -> None:
    """Restore the builtin defaults (test isolation)."""
    MACRO_REGIONS.clear()
    MACRO_REGIONS.update(builtin_macro_regions())
    _REGION_NAMES.clear()


# Display names for common codes (free-form codes beyond these are still valid).
KNOWN_CODES: dict[str, str] = {
    "GLOBAL": "Global",
    "ANZ": "Australia & New Zealand",
    "APAC": "Asia-Pacific",
    "NORTH_AMERICA": "North America",
    "LATAM": "Latin America",
    "EUROPE": "Europe",
    "MIDDLE_EAST": "Middle East",
    "AFRICA": "Africa",
    "AU": "Australia",
    "NZ": "New Zealand",
    "US": "United States",
    "GB": "United Kingdom",
    "AU-QLD": "Queensland",
    "AU-NSW": "New South Wales",
    "AU-VIC": "Victoria",
    "AU-WA": "Western Australia",
    "AU-SA": "South Australia",
    "AU-TAS": "Tasmania",
    "AU-NT": "Northern Territory",
    "AU-ACT": "Australian Capital Territory",
    "US-WA": "Washington State",
    "US-CA": "California",
    "US-TX": "Texas",
    "NZ-AUK": "Auckland",
    "AU-VIC-MELBOURNE": "Melbourne",
    "AU-QLD-BRISBANE": "Brisbane",
}

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_HIERARCHICAL_RE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9][A-Z0-9_]{0,49})+$")
# Named-region codes: ≥3 chars (no ISO-country collision), no hyphens (reserved for
# the hierarchy), letters/digits/underscores.
_REGION_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,49}$")


class GeographyMatchMode(enum.StrEnum):
    """Direction used when comparing source coverage with a requested geography."""

    OVERLAPS = "overlaps"
    COVERS = "covers"
    WITHIN = "within"
    EXACT = "exact"


class InvalidGeographyCode(ValueError):
    """Raised at configuration boundaries for malformed geography identifiers."""


def normalise_code(raw: str) -> str:
    return raw.strip().upper().replace(" ", "_")


def validate_code(raw: str) -> str:
    """Normalise and validate a macro, country, subdivision, or locality code.

    Core matching intentionally accepts an extensible locality suffix, while named macro
    regions must be registered explicitly. This catches typos at input boundaries without
    pretending to be a complete gazetteer.
    """
    code = normalise_code(raw)
    if not code:
        raise InvalidGeographyCode("geography code cannot be empty")
    if len(code) > 100:
        raise InvalidGeographyCode(f"geography code is too long: {raw!r}")
    if code in MACRO_REGIONS or _COUNTRY_RE.fullmatch(code) or _HIERARCHICAL_RE.fullmatch(code):
        return code
    raise InvalidGeographyCode(
        f"invalid geography code {raw!r}; use a registered macro, ISO country, "
        "or hierarchical code such as AU-QLD"
    )


def validate_region_definition(code_raw: str, members_raw: Iterable[str]) -> tuple[str, list[str]]:
    """Validate a named-region definition at input boundaries (region editor, API).

    Members must be countries, subdivisions, or localities — regions may not nest, and
    GLOBAL is not definable. Returns (code, sorted member codes).
    """
    code = normalise_code(code_raw)
    if code == "GLOBAL":
        raise InvalidGeographyCode("GLOBAL is built in and cannot be redefined")
    if not _REGION_CODE_RE.fullmatch(code):
        raise InvalidGeographyCode(
            f"invalid region code {code_raw!r}; use 3-50 chars of A-Z, 0-9, _ "
            "(no hyphens — those are reserved for country subdivisions)"
        )
    members: list[str] = []
    seen: set[str] = set()
    for raw in members_raw:
        member = normalise_code(raw)
        if not member:
            continue
        if not (_COUNTRY_RE.fullmatch(member) or _HIERARCHICAL_RE.fullmatch(member)):
            if member == "GLOBAL" or member in MACRO_REGIONS or _REGION_CODE_RE.fullmatch(member):
                raise InvalidGeographyCode(
                    f"region member {raw!r} looks like another named region; "
                    "regions may not nest — list countries or subdivisions directly"
                )
            raise InvalidGeographyCode(
                f"invalid region member {raw!r}; use ISO countries (AU) or "
                "hierarchical codes (US-WA)"
            )
        if member not in seen:
            seen.add(member)
            members.append(member)
    if not members:
        raise InvalidGeographyCode("a named region needs at least one member code")
    return code, sorted(members)


def normalise_codes(codes: list[str], *, validate: bool = False) -> list[str]:
    """Return stable, de-duplicated codes suitable for JSON persistence."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = validate_code(raw) if validate else normalise_code(raw)
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    return result


def expand_codes(codes: list[str]) -> set[str]:
    """Expand macro regions to country codes; pass other codes through."""
    out: set[str] = set()
    for raw in codes:
        code = normalise_code(raw)
        if code in MACRO_REGIONS:
            out.add(code)
            out.update(MACRO_REGIONS[code])
        else:
            out.add(code)
    return out


def parse_jurisdiction(raw: str | None, *, validate: bool = False) -> list[str]:
    """Split free-form jurisdiction strings ('AU/NZ', 'AU, NZ', 'global') into codes."""
    if not raw:
        return []
    parts = [p for chunk in raw.split("/") for p in chunk.split(",")]
    return normalise_codes([p for p in parts if p.strip()], validate=validate)


def _related(a: str, b: str) -> bool:
    """True when one code is an ancestor of the other (prefix on '-' boundaries)."""
    if a == b:
        return True
    return a.startswith(b + "-") or b.startswith(a + "-")


def _ancestor(ancestor: str, descendant: str) -> bool:
    return ancestor == descendant or descendant.startswith(ancestor + "-")


def _covers(covering: str, requested: str) -> bool:
    """Whether one explicit code covers the whole requested code.

    Named-region members may be countries or subdivisions, so coverage is judged by
    ancestry against each member, not by bare set inclusion.
    """
    if covering == "GLOBAL":
        return True
    if requested == "GLOBAL":
        return False
    covering_members = MACRO_REGIONS.get(covering)
    requested_members = MACRO_REGIONS.get(requested)
    if covering_members is not None:
        if requested_members is not None:
            if not requested_members:
                return False
            return all(
                any(_ancestor(member, wanted) for member in covering_members)
                for wanted in requested_members
            )
        return any(_ancestor(member, requested) for member in covering_members)
    if requested_members is not None:
        if not requested_members:
            return False
        return all(_ancestor(covering, member) for member in requested_members)
    return _ancestor(covering, requested)


def match_geography(
    item_codes: list[str],
    requested_codes: list[str],
    *,
    mode: GeographyMatchMode | str = GeographyMatchMode.OVERLAPS,
    include_unknown: bool = True,
) -> bool:
    """Match source geography against requested geography with explicit direction.

    An empty source geography is *unknown*, not global. ``include_unknown`` controls
    whether it is retained. An empty request means no geographic filter.
    """
    try:
        match_mode = GeographyMatchMode(mode)
    except ValueError as exc:
        raise ValueError(f"unknown geography match mode: {mode!r}") from exc

    raw_items = set(normalise_codes(item_codes))
    raw_requested = set(normalise_codes(requested_codes))
    if not raw_requested:
        return True
    if not raw_items:
        return include_unknown

    item_global = "GLOBAL" in raw_items
    requested_global = "GLOBAL" in raw_requested
    if match_mode == GeographyMatchMode.EXACT:
        return bool(raw_items & raw_requested)
    if match_mode == GeographyMatchMode.COVERS:
        if item_global:
            return True
        if requested_global:
            return False
    elif match_mode == GeographyMatchMode.WITHIN:
        if requested_global:
            return True
        if item_global:
            return False
    elif item_global or requested_global:
        return True

    if match_mode == GeographyMatchMode.COVERS:
        return any(_covers(item, wanted) for item in raw_items for wanted in raw_requested)
    if match_mode == GeographyMatchMode.WITHIN:
        return any(_covers(wanted, item) for item in raw_items for wanted in raw_requested)
    items = expand_codes(list(raw_items))
    requested = expand_codes(list(raw_requested))
    return any(_related(item, wanted) for item in items for wanted in requested)


def excluded_by(item_codes: list[str], exclude_codes: list[str]) -> bool:
    """True when a non-empty geographic footprint lies entirely inside the excluded area.

    Deliberately conservative: 'APAC minus China' still wants APAC-wide sources, so a
    source reaching beyond the exclusion is kept — only sources operating wholly inside
    it are dropped. Unknown footprints are never excluded (missing ≠ false; the scope's
    include_unknown flag governs those separately).
    """
    items = set(normalise_codes(item_codes))
    excludes = set(normalise_codes(exclude_codes))
    if not items or not excludes:
        return False
    return all(any(_covers(exclude, item) for exclude in excludes) for item in items)


def in_scope(item_codes: list[str], scope_codes: list[str]) -> bool:
    """Ancestor-or-descendant match between an item's codes and a scope's codes.

    GLOBAL on either side matches everything; empty item codes are treated as unknown
    and conservatively kept in scope (missing is not false, spec §6.3).
    """
    return match_geography(item_codes, scope_codes, include_unknown=True)


def describe(code: str) -> str:
    normalised = normalise_code(code)
    return _REGION_NAMES.get(normalised) or KNOWN_CODES.get(normalised, code)
