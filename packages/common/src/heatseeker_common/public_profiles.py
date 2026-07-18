"""Conservative normalisation for public business-facing social profile URLs.

These are company/source identities, never permission to scrape a platform. Personal
LinkedIn profiles, content-only routes that do not identify a stable account,
login/share routes, and generic platform homepages are deliberately rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass(frozen=True, slots=True)
class SocialProfile:
    platform: str
    url: str


_ALIASES = {
    "fb.com": "facebook.com",
    "m.facebook.com": "facebook.com",
    "www.facebook.com": "facebook.com",
    "www.instagram.com": "instagram.com",
    "www.linkedin.com": "linkedin.com",
    "m.linkedin.com": "linkedin.com",
    "www.youtube.com": "youtube.com",
    "m.youtube.com": "youtube.com",
    "www.tiktok.com": "tiktok.com",
    "twitter.com": "x.com",
    "www.twitter.com": "x.com",
    "www.x.com": "x.com",
    "www.threads.net": "threads.net",
    "www.pinterest.com": "pinterest.com",
    "www.reddit.com": "reddit.com",
}

_PLATFORM_HOST = {
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "linkedin": "linkedin.com",
    "youtube": "youtube.com",
    "tiktok": "tiktok.com",
    "x": "x.com",
    "threads": "threads.net",
    "pinterest": "pinterest.com",
    "reddit": "reddit.com",
}

_HOST_PLATFORM = {host: platform for platform, host in _PLATFORM_HOST.items()}

_RESERVED_HANDLES = {
    "about",
    "accounts",
    "ads",
    "business",
    "developers",
    "directory",
    "events",
    "explore",
    "groups",
    "hashtag",
    "help",
    "home",
    "intent",
    "login",
    "marketplace",
    "messages",
    "notifications",
    "p",
    "pages",
    "privacy",
    "reel",
    "search",
    "share",
    "sharer",
    "signup",
    "status",
    "terms",
    "watch",
}


def _handle_url(platform: str, raw: str) -> str:
    host = _PLATFORM_HOST[platform]
    handle = raw.strip().strip("/")
    if platform in {"instagram", "tiktok", "threads"}:
        handle = handle.removeprefix("@")
        path = f"/@{handle}" if platform in {"tiktok", "threads"} else f"/{handle}"
    elif platform == "linkedin":
        path = f"/company/{handle}"
    elif platform == "youtube":
        path = f"/@{handle.removeprefix('@')}"
    elif platform == "reddit":
        path = f"/r/{handle.removeprefix('r/')}"
    else:
        path = f"/{handle.removeprefix('@')}"
    return f"https://{host}{path}"


def normalise_social_profile_url(
    value: str, *, expected_platform: str | None = None
) -> SocialProfile:
    """Return a stable public-profile identity or raise ``ValueError``.

    When a mapping column is platform-specific, a bare handle is accepted. Full URLs
    must still point at the expected platform.
    """

    raw = value.strip()
    platform_hint = (expected_platform or "").strip().lower()
    if platform_hint and platform_hint not in _PLATFORM_HOST:
        raise ValueError(f"unsupported social platform: {expected_platform!r}")
    if "://" not in raw:
        possible_host = raw.split("/", 1)[0].lower()
        known_hosts = set(_ALIASES) | set(_HOST_PLATFORM)
        if platform_hint and possible_host not in known_hosts:
            raw = _handle_url(platform_hint, raw)
        else:
            raw = f"https://{raw}"

    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("social profile must be an absolute HTTP(S) URL or mapped handle")
    if parts.username or parts.password:
        raise ValueError("social profile URL must not contain credentials")
    host = _ALIASES.get(parts.hostname.lower(), parts.hostname.lower())
    platform = _HOST_PLATFORM.get(host)
    if platform is None or (platform_hint and platform != platform_hint):
        raise ValueError("URL does not match a supported public social platform")

    segments = [segment for segment in parts.path.split("/") if segment]
    query = ""
    if platform == "facebook" and segments[:1] == ["profile.php"]:
        profile_id = dict(parse_qsl(parts.query)).get("id", "").strip()
        if not profile_id:
            raise ValueError("Facebook profile.php URL requires an id")
        path = "/profile.php"
        query = urlencode({"id": profile_id})
    elif platform == "linkedin":
        if len(segments) < 2 or segments[0].lower() not in {"company", "school", "showcase"}:
            raise ValueError("LinkedIn discovery accepts organisation pages, not personal profiles")
        path = f"/{segments[0].lower()}/{segments[1].lower()}"
    elif platform == "youtube":
        if not segments or not (
            segments[0].startswith("@")
            or (len(segments) >= 2 and segments[0].lower() in {"channel", "c", "user"})
        ):
            raise ValueError("YouTube URL must identify a channel")
        path = (
            f"/@{segments[0].removeprefix('@').lower()}"
            if segments[0].startswith("@")
            else f"/{segments[0].lower()}/{segments[1]}"
        )
    elif platform == "reddit":
        if len(segments) < 2 or segments[0].lower() != "r":
            raise ValueError("Reddit discovery accepts public communities, not user profiles")
        path = f"/r/{segments[1].lower()}"
    else:
        if not segments:
            raise ValueError("generic platform homepages are not business profiles")
        handle = segments[0].removeprefix("@").strip().lower()
        if not handle or handle in _RESERVED_HANDLES:
            raise ValueError("URL identifies platform content/navigation, not a business profile")
        if platform in {"tiktok", "threads"} and not segments[0].startswith("@"):
            raise ValueError(f"{platform} profile path must begin with @")
        path = f"/@{handle}" if platform in {"tiktok", "threads"} else f"/{handle}"

    return SocialProfile(platform, urlunsplit(("https", host, path.rstrip("/"), query, "")))


def try_social_profile_url(value: str) -> SocialProfile | None:
    try:
        return normalise_social_profile_url(value)
    except (TypeError, ValueError):
        return None
