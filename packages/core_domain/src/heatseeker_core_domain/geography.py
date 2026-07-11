"""Generic geography codes and scope matching (spec §6.5: geography is a core concept).

Code forms (hierarchical, prefix-based):
  Macro region:  APAC, ANZ, NORTH_AMERICA, EUROPE, GLOBAL
  Country:       ISO 3166-1 alpha-2 (AU, NZ, US, GB, ...)
  Subdivision:   ISO 3166-2 (AU-QLD, US-WA, AU-VIC, ...)
  Locality:      subdivision + slug (AU-VIC-MELBOURNE) or country + slug (NZ-AUCKLAND)

Matching rule: a thing is *in scope* when its code is an ancestor OR descendant of any
scope code. A national source (AU) covers Queensland research (AU-QLD); a Queensland
source is relevant to an Australia-wide scope. GLOBAL matches everything.

Directional callers can choose exact, covers, or within instead of symmetric overlap;
macro-region coverage is set-aware (AU does not claim to cover all of ANZ).
"""

import enum
import re

# Macro regions expand to country sets. Editable/extensible; not exhaustive gazetteers.
MACRO_REGIONS: dict[str, set[str]] = {
    "GLOBAL": set(),  # special-cased: matches everything
    "ANZ": {"AU", "NZ"},
    "APAC": {
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
    },
    "NORTH_AMERICA": {"US", "CA", "MX"},
    "EUROPE": {
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
    },
}

# Display names for common codes (free-form codes beyond these are still valid).
KNOWN_CODES: dict[str, str] = {
    "GLOBAL": "Global",
    "ANZ": "Australia & New Zealand",
    "APAC": "Asia-Pacific",
    "NORTH_AMERICA": "North America",
    "EUROPE": "Europe",
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
    """Whether one explicit code covers the whole requested code."""
    if covering == "GLOBAL":
        return True
    if requested == "GLOBAL":
        return False
    covering_macro = MACRO_REGIONS.get(covering)
    requested_macro = MACRO_REGIONS.get(requested)
    if covering_macro is not None:
        if requested_macro is not None:
            return requested_macro <= covering_macro
        return requested.split("-", 1)[0] in covering_macro
    if requested_macro is not None:
        return False
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


def in_scope(item_codes: list[str], scope_codes: list[str]) -> bool:
    """Ancestor-or-descendant match between an item's codes and a scope's codes.

    GLOBAL on either side matches everything; empty item codes are treated as unknown
    and conservatively kept in scope (missing is not false, spec §6.3).
    """
    return match_geography(item_codes, scope_codes, include_unknown=True)


def describe(code: str) -> str:
    return KNOWN_CODES.get(normalise_code(code), code)
