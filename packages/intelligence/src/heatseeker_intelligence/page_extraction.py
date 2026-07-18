"""Deterministic company-page signal extraction — no AI required (§41.19).

Pure functions turning distilled page text into structured signals: public contact
routes, Australian street addresses, pack-vocabulary service/system mentions, and
in-house design capability phrasing. Consumers record the results as observations with
page-level provenance; nothing here fetches or writes.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from selectolax.parser import HTMLParser

EXTRACTOR_VERSION = "pages/0.1"

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_EMAIL_JUNK = re.compile(
    r"\.(png|jpe?g|gif|svg|webp)$|@(example|sentry|wixpress|placeholder)\.", re.IGNORECASE
)
# AU phone shapes: 02/03/07/08 + 8 digits, 04xx mobiles, 13/1300/1800 numbers, +61 forms.
_PHONE_RE = re.compile(
    r"(?<![\d/])(?:\+?61[\s.-]?(?:\(0\))?[\s.-]?|\(0[2378]\)[\s.-]?|0[2378][\s.-]?|04\d{2}[\s.-]?)"
    r"\d{3,4}[\s.-]?\d{3,4}(?![\d%])|(?<![\d/])1[38]00[\s.-]?\d{3}[\s.-]?\d{3}(?![\d%])"
    r"|(?<![\d/])13[\s.-]?\d{2}[\s.-]?\d{2}(?![\d%])"
)
_STATE = r"(?:NSW|QLD|VIC|WA|SA|TAS|NT|ACT)"
_STREET_WORD = (
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Court|Ct|Place|Pl|Parade|Pde|Highway|"
    r"Hwy|Way|Crescent|Cres|Boulevard|Blvd|Circuit|Cct|Lane|Ln|Terrace|Tce)"
)
_ADDRESS_RE = re.compile(
    rf"(\d+[\w\-/]*\s+[A-Za-z][A-Za-z\s']{{2,40}}?{_STREET_WORD})\.?,?\s+"
    rf"([A-Za-z][A-Za-z\s']{{2,30}}?),?\s+({_STATE})\s*,?\s*(\d{{4}})",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(
    r"\b(ABN|ACN|NZBN)\s*(?:No\.?\s*)?[:#]?\s*((?:\d[\s.-]?){8,14}\d)\b",
    re.IGNORECASE,
)
_IDENTIFIER_LENGTHS = {"abn": 11, "acn": 9, "nzbn": 13}

# Phrases that indicate the company designs/drafts in-house (§19.3 negative-need signal).
_INHOUSE_DESIGN_RE = re.compile(
    r"in[\s-]?house\s+(?:scaffold\s+)?(?:design|draft\w*|engineer\w*)"
    r"|our\s+(?:design|drafting|engineering)\s+(?:team|department|office)"
    r"|our\s+(?:in[\s-]?house\s+)?(?:designers|drafters|draftsmen|engineers)\b",
    re.IGNORECASE,
)

_ROLE_LOCAL_PARTS = {
    "estimating", "tenders", "design", "drafting", "engineering",
    "projects", "operations", "commercial", "procurement",
}


@dataclass(slots=True)
class PageSignals:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    addresses: list[dict] = field(default_factory=list)  # street/locality/state/postcode
    identifiers: list[tuple[str, str]] = field(default_factory=list)
    service_hits: list[tuple[str, str]] = field(default_factory=list)  # (id, matched text)
    system_hits: list[tuple[str, str]] = field(default_factory=list)
    archetype_hits: list[tuple[str, str]] = field(default_factory=list)
    inhouse_design_phrases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PageMetadata:
    description: str | None = None
    contact_form_url: str | None = None
    links: list[tuple[str, str]] = field(default_factory=list)


def _dedupe(values):
    seen, out = set(), []
    for value in values:
        key = value.casefold() if isinstance(value, str) else value
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _normalise_phone(raw: str) -> str:
    return " ".join(raw.split())


def _vocabulary_patterns(
    vocabulary: dict[str, str | list[str]],
) -> list[tuple[str, re.Pattern]]:
    """id → compiled word-boundary pattern over label, id words, and pack synonyms."""
    patterns = []
    for item_id, value in vocabulary.items():
        labels = [value] if isinstance(value, str) else list(value)
        variants = {v for v in (*labels, item_id.replace("_", " ")) if v}
        alternates = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
        patterns.append((item_id, re.compile(rf"\b(?:{alternates})\b", re.IGNORECASE)))
    return patterns


def extract_signals(
    text: str,
    *,
    services: dict[str, str | list[str]] | None = None,
    systems: dict[str, str | list[str]] | None = None,
    archetypes: dict[str, str | list[str]] | None = None,
) -> PageSignals:
    """Extract everything we can defend from one page of distilled text."""
    signals = PageSignals()
    if not text:
        return signals

    signals.emails = _dedupe(
        match.group(0).lower()
        for match in _EMAIL_RE.finditer(text)
        if not _EMAIL_JUNK.search(match.group(0))
    )
    signals.phones = _dedupe(
        _normalise_phone(match.group(0)) for match in _PHONE_RE.finditer(text)
    )
    for match in _IDENTIFIER_RE.finditer(text):
        scheme = match.group(1).casefold()
        value = "".join(character for character in match.group(2) if character.isdigit())
        if len(value) == _IDENTIFIER_LENGTHS[scheme]:
            signals.identifiers.append((scheme, value))

    seen_addresses = set()
    for match in _ADDRESS_RE.finditer(text):
        street, locality, state, postcode = (part.strip() for part in match.groups())
        key = (street.casefold(), postcode)
        if key in seen_addresses:
            continue
        seen_addresses.add(key)
        signals.addresses.append(
            {
                "street": street,
                "locality": locality.title(),
                "state": state.upper(),
                "postcode": postcode,
            }
        )

    for target, vocabulary in (
        ("service_hits", services), ("system_hits", systems), ("archetype_hits", archetypes)
    ):
        if not vocabulary:
            continue
        hits = []
        for item_id, pattern in _vocabulary_patterns(vocabulary):
            found = pattern.search(text)
            if found:
                hits.append((item_id, found.group(0)))
        setattr(signals, target, hits)

    signals.inhouse_design_phrases = _dedupe(
        match.group(0) for match in _INHOUSE_DESIGN_RE.finditer(text)
    )
    return signals


def is_role_email(email: str) -> bool:
    return email.split("@", 1)[0] in _ROLE_LOCAL_PARTS


def extract_page_metadata(html: str, page_url: str) -> PageMetadata:
    """Extract navigation and page-level fields that disappear during text distillation."""
    tree = HTMLParser(html)
    description = None
    for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
        node = tree.css_first(selector)
        value = (node.attributes.get("content", "").strip() if node else "")
        if 30 <= len(value) <= 1000:
            description = " ".join(value.split())
            break

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in tree.css("a[href]"):
        absolute = urljoin(page_url, node.attributes.get("href", "")).split("#", 1)[0]
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.netloc or absolute in seen:
            continue
        seen.add(absolute)
        links.append((absolute, " ".join((node.text() or "").split())[:300]))

    contact_form_url = None
    if tree.css_first("form") is not None:
        haystack = f"{urlsplit(page_url).path} {tree.body.text() if tree.body else ''}".casefold()
        if any(term in haystack for term in ("contact", "enquir", "quote", "estimate")):
            contact_form_url = page_url
    return PageMetadata(
        description=description,
        contact_form_url=contact_form_url,
        links=links,
    )
