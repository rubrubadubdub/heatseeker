"""Polite HTTP retrieval: identified user-agent, size cap, conditional GET (spec §11.2, §11.5)."""

from collections.abc import Callable
from dataclasses import dataclass
from email.message import Message
from pathlib import PurePath
from urllib.parse import urljoin, urlsplit

import httpx
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    content: bytes
    content_type: str | None
    etag: str | None
    last_modified: str | None
    not_modified: bool
    fetched_at: str
    retry_after: str | None = None  # server throttle hint (429/503), honoured by collect
    content_disposition: str | None = None
    redirect_chain: tuple[str, ...] = ()


class FetchTooLargeError(Exception):
    pass


class FetchRedirectBlockedError(Exception):
    pass


def http_client_kwargs(settings: Settings, transport: httpx.BaseTransport | None) -> dict:
    """Central egress kwargs for httpx.Client construction.

    An injected transport (used by tests) takes precedence; otherwise the default
    transport applies. Egress construction is centralised at this single seam so an
    alternate transport can be introduced here later without touching call sites.
    """
    if transport is not None:
        return {"transport": transport}
    return {}


def response_filename(content_disposition: str | None, final_url: str) -> str | None:
    """Return a bounded basename from Content-Disposition or the final URL path."""
    filename = None
    if content_disposition:
        message = Message()
        message["Content-Disposition"] = content_disposition
        filename = message.get_filename()
    if not filename:
        filename = PurePath(urlsplit(final_url).path).name or None
    if not filename:
        return None
    safe = PurePath(filename.replace("\\", "/")).name
    safe = "".join(char for char in safe if char >= " " and char != "\x7f").strip()
    return safe[:500] or None


def fetch_url(
    settings: Settings,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    transport: httpx.BaseTransport | None = None,
    max_bytes: int | None = None,
    redirect_validator: Callable[[str], bool] | None = None,
    max_redirects: int = 5,
) -> FetchResult:
    """GET one URL with the configured identity. 304s return not_modified=True.

    transport is injectable for tests (httpx.MockTransport). Raises httpx errors on
    network failure — callers record them as source failures.
    """
    headers = {"User-Agent": settings.crawler_user_agent, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    limit = max_bytes if max_bytes is not None else settings.fetch_max_bytes
    current_url = url
    redirect_chain = [url]
    with httpx.Client(
        timeout=settings.fetch_timeout_seconds,
        follow_redirects=False,
        headers=headers,
        **http_client_kwargs(settings, transport),
    ) as client:
        for _redirect_count in range(max_redirects + 1):
            with client.stream("GET", current_url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        break
                    next_url = urljoin(str(response.url), location)
                    parts = urlsplit(next_url)
                    if (
                        parts.scheme not in {"http", "https"}
                        or not parts.netloc
                        or parts.username
                        or parts.password
                        or (redirect_validator is not None and not redirect_validator(next_url))
                    ):
                        raise FetchRedirectBlockedError(
                            f"redirect from {current_url} to disallowed URL {next_url}"
                        )
                    current_url = next_url
                    redirect_chain.append(next_url)
                    continue

                if response.status_code == 304:
                    return FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status_code=304,
                        content=b"",
                        content_type=None,
                        etag=etag,
                        last_modified=last_modified,
                        not_modified=True,
                        fetched_at=utc_now().isoformat(),
                        redirect_chain=tuple(redirect_chain),
                    )
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        declared_bytes = int(declared)
                    except ValueError:
                        declared_bytes = None
                    if declared_bytes is not None and declared_bytes > limit:
                        raise FetchTooLargeError(f"{url}: declared {declared} bytes")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise FetchTooLargeError(f"{url}: exceeded {limit} bytes")
                    chunks.append(chunk)
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content=b"".join(chunks),
                    content_type=response.headers.get("Content-Type"),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    not_modified=False,
                    fetched_at=utc_now().isoformat(),
                    retry_after=response.headers.get("Retry-After"),
                    content_disposition=response.headers.get("Content-Disposition"),
                    redirect_chain=tuple(redirect_chain),
                )
        raise FetchRedirectBlockedError(f"{url}: exceeded {max_redirects} redirects")
