"""Polite HTTP retrieval: identified user-agent, size cap, conditional GET (spec §11.2, §11.5)."""

from dataclasses import dataclass

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


class FetchTooLargeError(Exception):
    pass


def fetch_url(
    settings: Settings,
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    transport: httpx.BaseTransport | None = None,
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

    with (
        httpx.Client(
            timeout=settings.fetch_timeout_seconds,
            follow_redirects=True,
            headers=headers,
            transport=transport,
        ) as client,
        client.stream("GET", url) as response,
    ):
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
            )
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > settings.fetch_max_bytes:
            raise FetchTooLargeError(f"{url}: declared {declared} bytes")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > settings.fetch_max_bytes:
                raise FetchTooLargeError(f"{url}: exceeded {settings.fetch_max_bytes} bytes")
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
        )
