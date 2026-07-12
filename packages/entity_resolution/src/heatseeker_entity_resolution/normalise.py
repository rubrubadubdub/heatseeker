"""Deterministic normalisation for entity matching keys.

Conservative on purpose: these keys feed match *signals*, not automatic merges, so a
missed normalisation costs a review candidate, never a wrong merge.
"""

import re
import unicodedata
from urllib.parse import urlsplit

# Legal-form suffixes only. Brand-meaningful tails ("Group", "Holdings", "Co") stay —
# stripping them would collapse genuinely distinct organisations.
_LEGAL_SUFFIXES = {
    "pty",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "llc",
    "plc",
    "corp",
    "corporation",
    "gmbh",
    "bv",
    "nv",
}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DIGITS = re.compile(r"\d")


def normalise_name(name: str) -> str:
    """Lowercased, punctuation-free, legal-suffix-stripped comparison key."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    text = text.casefold().replace("&", " and ")
    tokens = [t for t in _NON_ALNUM.split(text) if t]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def name_tokens(normalised_name: str) -> set[str]:
    return set(normalised_name.split())


def normalise_domain(value: str) -> str:
    """Bare lowercase host: scheme, path, port, and leading www. removed."""
    candidate = value.strip().casefold()
    if "//" not in candidate:
        candidate = f"//{candidate}"
    host = urlsplit(candidate).hostname or ""
    return host.removeprefix("www.")


def phone_match_key(value: str) -> str | None:
    """Last 8 digits — comparable across +61/0-prefix formats without country logic."""
    digits = "".join(_DIGITS.findall(value))
    if len(digits) < 8:
        return None
    return digits[-8:]


def normalise_identifier(value: str) -> str:
    """Uppercase with separators removed (ABN '51 824 753 556' == '51824753556')."""
    return re.sub(r"[\s\-./]", "", value).upper()
