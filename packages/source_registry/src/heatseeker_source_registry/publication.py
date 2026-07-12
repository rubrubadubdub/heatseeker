"""Conservative claimed-publication timestamp extraction from HTML metadata."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from selectolax.parser import HTMLParser

_META_KEYS = {
    "article:published_time",
    "og:published_time",
    "datepublished",
    "date",
    "pubdate",
}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(cleaned)
        except (TypeError, ValueError):
            return None
    # A timezone-free date/time would invent an offset; leave it unknown.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _json_dates(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"datepublished", "uploaddate"} and isinstance(child, str):
                found.append(child)
            else:
                found.extend(_json_dates(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_json_dates(child))
    return found


def extract_claimed_published_at(raw: bytes) -> datetime | None:
    tree = HTMLParser(raw)
    candidates: list[str] = []
    for node in tree.css("meta[content]"):
        key = (
            (
                node.attributes.get("property")
                or node.attributes.get("name")
                or node.attributes.get("itemprop")
                or ""
            )
            .strip()
            .lower()
        )
        if key in _META_KEYS:
            candidates.append(node.attributes.get("content") or "")
    for node in tree.css("time[datetime]"):
        candidates.append(node.attributes.get("datetime") or "")
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            candidates.extend(_json_dates(json.loads(node.text())))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    parsed = [stamp for value in candidates if (stamp := _parse_timestamp(value)) is not None]
    return min(parsed) if parsed else None
