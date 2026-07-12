from datetime import UTC, datetime

from heatseeker_source_registry.publication import extract_claimed_published_at


def test_extracts_earliest_claimed_timestamp_from_metadata_and_json_ld():
    raw = b"""
    <meta property="article:published_time" content="2026-07-11T12:30:00+10:00">
    <script type="application/ld+json">
      {"@type":"NewsArticle","datePublished":"2026-07-11T01:00:00Z"}
    </script>
    """
    assert extract_claimed_published_at(raw) == datetime(2026, 7, 11, 1, tzinfo=UTC)


def test_ignores_unzoned_or_malformed_dates_instead_of_inventing_precision():
    raw = b'<time datetime="2026-07-11">today</time><meta name="date" content="not-a-date">'
    assert extract_claimed_published_at(raw) is None
