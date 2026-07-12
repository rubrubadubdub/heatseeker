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
_BLOCKING_STOP_WORDS = {"a", "an", "and", "for", "of", "the"}
_PUBLIC_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "outlook.com",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
}


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


def blocking_name_tokens(normalised_name: str) -> set[str]:
    """Useful name tokens for candidate generation, without generic join words."""
    return {
        token
        for token in normalised_name.split()
        if token not in _BLOCKING_STOP_WORDS and len(token) >= 2
    }


def normalise_domain(value: str) -> str:
    """Bare lowercase host: scheme, path, port, and leading www. removed."""
    candidate = value.strip().casefold()
    if "//" not in candidate:
        candidate = f"//{candidate}"
    host = urlsplit(candidate).hostname or ""
    return host.removeprefix("www.")


def email_domain(value: str) -> str | None:
    """Normalised domain from a syntactically plausible email address."""
    _local, separator, domain = value.strip().casefold().rpartition("@")
    host = normalise_domain(domain) if separator else ""
    return host if host and host not in _PUBLIC_EMAIL_DOMAINS else None


def normalise_address(parts: list[str | None]) -> str | None:
    """Conservative exact-address key; locality alone is intentionally insufficient."""
    populated = [part.strip() for part in parts if part and part.strip()]
    if len(populated) < 2:
        return None
    return normalise_name(" ".join(populated)) or None


def phone_match_key(value: str) -> str | None:
    """Last 8 digits — comparable across +61/0-prefix formats without country logic."""
    digits = "".join(_DIGITS.findall(value))
    if len(digits) < 8:
        return None
    return digits[-8:]


def normalise_identifier(value: str) -> str:
    """Uppercase with separators removed (ABN '51 824 753 556' == '51824753556')."""
    return re.sub(r"[\s\-./]", "", value).upper()
