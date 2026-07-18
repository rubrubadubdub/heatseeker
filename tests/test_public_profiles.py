"""Public social profiles are stable identities, not collection permissions."""

import pytest
from heatseeker_common.public_profiles import normalise_social_profile_url


@pytest.mark.parametrize(
    ("value", "platform", "expected"),
    [
        ("Acme.Scaffold", "instagram", "https://instagram.com/acme.scaffold"),
        ("https://fb.com/AcmeScaffold/", "facebook", "https://facebook.com/acmescaffold"),
        (
            "https://www.linkedin.com/company/acme-scaffold/posts/",
            "linkedin",
            "https://linkedin.com/company/acme-scaffold",
        ),
        ("@AcmeScaffold", "youtube", "https://youtube.com/@acmescaffold"),
        ("https://twitter.com/AcmeScaffold/status/1", "x", "https://x.com/acmescaffold"),
        ("r/scaffolding", "reddit", "https://reddit.com/r/scaffolding"),
    ],
)
def test_profiles_normalise_to_stable_account_identities(value, platform, expected):
    profile = normalise_social_profile_url(value, expected_platform=platform)
    assert profile.platform == platform
    assert profile.url == expected


@pytest.mark.parametrize(
    ("value", "platform"),
    [
        ("https://instagram.com/", None),
        ("https://instagram.com/p/ABC123", None),
        ("https://linkedin.com/in/some-person", None),
        ("https://reddit.com/user/some-person", None),
        ("https://facebook.com/pages/acme/123", None),
        ("https://user:secret@instagram.com/acme", None),
        ("https://facebook.com/acme", "instagram"),
    ],
)
def test_non_company_or_mismatched_routes_are_rejected(value, platform):
    with pytest.raises(ValueError):
        normalise_social_profile_url(value, expected_platform=platform)
