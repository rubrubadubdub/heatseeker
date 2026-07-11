"""Time helpers. Convention: timezone-aware UTC datetimes everywhere.

SQLite stores datetimes as ISO strings; mixing aware and naive values breaks
comparisons, so every persisted datetime must come from utc_now() or be normalised.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)
