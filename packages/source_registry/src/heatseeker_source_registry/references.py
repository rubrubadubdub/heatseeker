"""Versioned, deterministic HTML evidence-reference extraction.

This module only describes references found in one immutable HTML document.  It does
not decide whether a target is in crawl scope, permitted by policy, or worth fetching;
those decisions belong to the crawler.  Repeated URLs are intentionally retained so a
caller can preserve every distinct DOM occurrence and its surrounding evidence context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from heatseeker_source_registry.identity import canonicalise_url

REFERENCE_EXTRACTOR_VERSION = "references/0.1"

ReferenceKind = Literal["navigation", "document", "image"]
ExpectedContent = Literal["html", "document", "image"]

_MAX_URL_CHARS = 2_000
_MAX_SHORT_CONTEXT = 500
_MAX_CAPTION = 1_000

_DOCUMENT_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".docm",
    ".dot",
    ".dotx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".ods",
    ".csv",
    ".tsv",
    ".ppt",
    ".pptx",
    ".pptm",
    ".pps",
    ".ppsx",
    ".odt",
    ".odp",
    ".rtf",
    ".txt",
    ".json",
    ".xml",
    ".zip",
    ".7z",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".gz",
)
_IMAGE_EXTENSIONS = (
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
)
_KNOWN_NON_DOCUMENT_EXTENSIONS = (
    *_IMAGE_EXTENSIONS,
    ".css",
    ".htm",
    ".html",
    ".js",
    ".m4a",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".ogg",
    ".swf",
    ".wav",
    ".webm",
)
_EXECUTABLE_EXTENSIONS = (
    ".apk",
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dmg",
    ".exe",
    ".msi",
    ".ps1",
    ".scr",
)

_DOCUMENT_MIME_TYPES = {
    "application/epub+zip",
    "application/json",
    "application/msword",
    "application/pdf",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
    "application/xml",
    "application/zip",
    "text/csv",
    "text/plain",
    "text/rtf",
    "text/tab-separated-values",
    "text/xml",
}

_OG_IMAGE_KEYS = {"og:image", "og:image:url", "og:image:secure_url"}
_TWITTER_IMAGE_KEYS = {"twitter:image", "twitter:image:src"}


@dataclass(frozen=True, slots=True)
class ReferenceCandidate:
    """One immutable DOM occurrence that may become collected evidence.

    ``url`` is the absolute URL produced by HTML base-URL resolution, while
    ``normalised_url`` is its conservative stable identity. ``raw_url`` preserves the
    exact attribute/srcset value for provenance. Context fields are bounded plain text,
    keeping the value object immutable and safe to persist as structured metadata.
    """

    url: str
    normalised_url: str
    raw_url: str
    kind: ReferenceKind
    expected_content: ExpectedContent
    rule: str
    ordinal: int
    source_attribute: str
    anchor_text: str | None = None
    alt_text: str | None = None
    title_text: str | None = None
    caption: str | None = None
    nearby_heading: str | None = None
    declared_type: str | None = None
    srcset_descriptor: str | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedURL:
    raw_url: str
    url: str
    normalised_url: str


@dataclass(frozen=True, slots=True)
class _ImageSelection:
    resolved: _ResolvedURL
    rule: str
    source_attribute: str
    declared_type: str | None = None
    descriptor: str | None = None
    score: tuple[int, float] = (0, 0.0)


def _clean_text(value: str | None, limit: int = _MAX_SHORT_CONTEXT) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned[:limit] or None


def _attribute(node: Node, name: str) -> str | None:
    value = node.attributes.get(name)
    return value if isinstance(value, str) else None


def _node_text(node: Node, limit: int = _MAX_SHORT_CONTEXT) -> str | None:
    return _clean_text(node.text(separator=" ", strip=True), limit)


def _normalise_mime(value: str | None) -> str | None:
    cleaned = _clean_text(value, 200)
    return cleaned.split(";", 1)[0].strip().lower() if cleaned else None


def _resolve_url(base_url: str, raw_url: str | None) -> _ResolvedURL | None:
    raw = (raw_url or "").strip()
    if not raw or raw.startswith("#") or len(raw) > _MAX_URL_CHARS:
        return None
    if any(ord(character) < 32 for character in raw):
        return None

    try:
        absolute = urljoin(base_url, raw)
        parts = urlsplit(absolute)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        normalised = canonicalise_url(absolute)
    except (UnicodeError, ValueError):
        return None
    if len(absolute) > _MAX_URL_CHARS or len(normalised) > _MAX_URL_CHARS:
        return None
    return _ResolvedURL(raw_url=raw, url=absolute, normalised_url=normalised)


def _document_base(tree: HTMLParser, base_url: str) -> str:
    # HTML defines the first base[href] as authoritative. An unsafe/invalid first base
    # is ignored rather than allowing a later element to change resolution semantics.
    base = tree.css_first("base[href]")
    if base is None:
        return base_url
    resolved = _resolve_url(base_url, _attribute(base, "href"))
    return resolved.url if resolved is not None else base_url


def _url_has_extension(url: str, extensions: tuple[str, ...]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    candidates = [unquote(parts.path).lower()]
    candidates.extend(unquote(value).lower() for _, value in parse_qsl(parts.query))
    return any(candidate.endswith(extensions) for candidate in candidates)


def _looks_like_document(url: str, declared_type: str | None) -> bool:
    return bool(
        declared_type in _DOCUMENT_MIME_TYPES
        or (declared_type and declared_type.startswith("application/vnd.ms-"))
        or _url_has_extension(url, _DOCUMENT_EXTENSIONS)
    )


def _looks_like_image(url: str, declared_type: str | None) -> bool:
    return bool(
        (declared_type and declared_type.startswith("image/"))
        or _url_has_extension(url, _IMAGE_EXTENSIONS)
    )


def _is_executable(url: str) -> bool:
    return _url_has_extension(url, _EXECUTABLE_EXTENSIONS)


def _embedded_document(url: str, declared_type: str | None, *, assume_unknown: bool) -> bool:
    if _looks_like_document(url, declared_type):
        return True
    if _looks_like_image(url, declared_type):
        return False
    if declared_type and declared_type.startswith(("audio/", "video/", "text/html")):
        return False
    if _url_has_extension(url, _KNOWN_NON_DOCUMENT_EXTENSIONS):
        return False
    return assume_unknown


def _figure_caption(node: Node) -> str | None:
    current: Node | None = node
    while current is not None:
        tag = (current.tag or "").lower()
        if tag == "figure":
            caption = current.css_first("figcaption")
            return _node_text(caption, _MAX_CAPTION) if caption is not None else None
        if tag in {"body", "html"}:
            return None
        current = current.parent
    return None


def _picture_ancestor(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        tag = (current.tag or "").lower()
        if tag == "picture":
            return current
        if tag in {"body", "html"}:
            return None
        current = current.parent
    return None


def _parse_srcset(value: str) -> list[tuple[str, str | None]]:
    """Parse the useful URL/descriptor subset of HTML srcset.

    URL tokens end at whitespace, not commas, which keeps commas inside data URLs and
    query strings together. This is deliberately extraction-only: invalid descriptors
    receive fallback rank and unsafe URL schemes are rejected during resolution.
    """

    candidates: list[tuple[str, str | None]] = []
    position = 0
    length = len(value)
    while position < length:
        while position < length and (value[position].isspace() or value[position] == ","):
            position += 1
        if position >= length:
            break

        start = position
        while position < length and not value[position].isspace():
            position += 1
        raw_url = value[start:position]

        # A no-descriptor candidate commonly ends in its delimiter comma. Commas inside
        # a URL stay intact because only trailing commas are removed here.
        if raw_url.endswith(","):
            raw_url = raw_url.rstrip(",")
            if raw_url:
                candidates.append((raw_url, None))
            continue

        while position < length and value[position].isspace():
            position += 1
        descriptor_start = position
        while position < length and value[position] != ",":
            position += 1
        descriptor = _clean_text(value[descriptor_start:position], 50)
        if position < length and value[position] == ",":
            position += 1
        if raw_url:
            candidates.append((raw_url, descriptor))
    return candidates


def _descriptor_score(descriptor: str | None) -> tuple[int, float]:
    if not descriptor:
        return (0, 1.0)
    token = descriptor.split()[0].lower()
    try:
        if token.endswith("w"):
            return (2, float(token[:-1]))
        if token.endswith("x"):
            return (1, float(token[:-1]))
    except ValueError:
        pass
    return (0, 0.0)


def _best_srcset(
    base_url: str,
    value: str | None,
    *,
    rule: str,
    source_attribute: str,
    declared_type: str | None = None,
) -> _ImageSelection | None:
    if not value:
        return None
    best: _ImageSelection | None = None
    for raw_url, descriptor in _parse_srcset(value):
        resolved = _resolve_url(base_url, raw_url)
        if resolved is None or _is_executable(resolved.url):
            continue
        score = _descriptor_score(descriptor)
        selection = _ImageSelection(
            resolved=resolved,
            rule=rule,
            source_attribute=source_attribute,
            declared_type=declared_type,
            descriptor=descriptor,
            score=score,
        )
        if best is None or selection.score > best.score:
            best = selection
    return best


def _select_image(base_url: str, node: Node) -> _ImageSelection | None:
    selections: list[_ImageSelection] = []
    picture = _picture_ancestor(node)
    if picture is not None:
        for source in picture.css("source"):
            declared_type = _normalise_mime(_attribute(source, "type"))
            for attribute in ("data-srcset", "srcset"):
                selection = _best_srcset(
                    base_url,
                    _attribute(source, attribute),
                    rule="picture-srcset",
                    source_attribute=attribute,
                    declared_type=declared_type,
                )
                if selection is not None:
                    selections.append(selection)

    for attribute in ("data-srcset", "srcset"):
        selection = _best_srcset(
            base_url,
            _attribute(node, attribute),
            rule=f"img-{attribute}",
            source_attribute=attribute,
        )
        if selection is not None:
            selections.append(selection)

    if selections:
        return max(selections, key=lambda selection: selection.score)

    for attribute in ("data-src", "src"):
        raw_url = _attribute(node, attribute)
        resolved = _resolve_url(base_url, raw_url)
        if resolved is not None and not _is_executable(resolved.url):
            return _ImageSelection(
                resolved=resolved,
                rule=f"img-{attribute}",
                source_attribute=attribute,
            )
    return None


def _meta_value(tree: HTMLParser, keys: set[str]) -> str | None:
    for node in tree.css("meta[content]"):
        key = (_attribute(node, "property") or _attribute(node, "name") or "").strip().lower()
        if key in keys:
            return _attribute(node, "content")
    return None


def _candidate(
    resolved: _ResolvedURL,
    *,
    kind: ReferenceKind,
    expected_content: ExpectedContent,
    rule: str,
    ordinal: int,
    source_attribute: str,
    node: Node,
    nearby_heading: str | None,
    anchor_text: str | None = None,
    alt_text: str | None = None,
    declared_type: str | None = None,
    srcset_descriptor: str | None = None,
) -> ReferenceCandidate:
    return ReferenceCandidate(
        url=resolved.url,
        normalised_url=resolved.normalised_url,
        raw_url=resolved.raw_url,
        kind=kind,
        expected_content=expected_content,
        rule=rule,
        ordinal=ordinal,
        source_attribute=source_attribute,
        anchor_text=_clean_text(anchor_text),
        alt_text=_clean_text(alt_text),
        title_text=_clean_text(_attribute(node, "title")),
        caption=_figure_caption(node),
        nearby_heading=nearby_heading,
        declared_type=declared_type,
        srcset_descriptor=_clean_text(srcset_descriptor, 50),
    )


def extract_references(
    base_url: str, raw: bytes, max_images_per_page: int = 12
) -> list[ReferenceCandidate]:
    """Extract bounded, versionable evidence references from an HTML document.

    Navigation and document occurrences are not deduplicated or image-budgeted. Images
    are returned in DOM order up to ``max_images_per_page``. All safe external HTTP(S)
    references are returned; crawl-origin and policy decisions intentionally happen later.
    """

    if isinstance(max_images_per_page, bool) or not isinstance(max_images_per_page, int):
        raise TypeError("max_images_per_page must be an integer")
    if max_images_per_page < 0:
        raise ValueError("max_images_per_page must be non-negative")
    try:
        canonicalise_url(base_url)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("base_url must be an absolute credential-free HTTP(S) URL") from exc

    tree = HTMLParser(raw)
    document_base = _document_base(tree, base_url)
    og_alt = _clean_text(_meta_value(tree, {"og:image:alt"}))
    twitter_alt = _clean_text(_meta_value(tree, {"twitter:image:alt"}))

    references: list[ReferenceCandidate] = []
    nearby_heading: str | None = None
    images_returned = 0

    for ordinal, node in enumerate(tree.css("*")):
        tag = (node.tag or "").lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            nearby_heading = _node_text(node)

        if tag == "a":
            resolved = _resolve_url(document_base, _attribute(node, "href"))
            if resolved is None or _is_executable(resolved.url):
                continue
            declared_type = _normalise_mime(_attribute(node, "type"))
            is_document = "download" in node.attributes or _looks_like_document(
                resolved.url, declared_type
            )
            references.append(
                _candidate(
                    resolved,
                    kind="document" if is_document else "navigation",
                    expected_content="document" if is_document else "html",
                    rule="anchor-document" if is_document else "anchor-navigation",
                    ordinal=ordinal,
                    source_attribute="href",
                    node=node,
                    nearby_heading=nearby_heading,
                    anchor_text=_node_text(node),
                    declared_type=declared_type,
                )
            )
            continue

        if tag == "img":
            if images_returned >= max_images_per_page:
                continue
            selection = _select_image(document_base, node)
            if selection is None:
                continue
            references.append(
                _candidate(
                    selection.resolved,
                    kind="image",
                    expected_content="image",
                    rule=selection.rule,
                    ordinal=ordinal,
                    source_attribute=selection.source_attribute,
                    node=node,
                    nearby_heading=nearby_heading,
                    alt_text=_attribute(node, "alt"),
                    declared_type=selection.declared_type,
                    srcset_descriptor=selection.descriptor,
                )
            )
            images_returned += 1
            continue

        if tag in {"object", "embed"}:
            source_attribute = "data" if tag == "object" else "src"
            resolved = _resolve_url(document_base, _attribute(node, source_attribute))
            if resolved is None or _is_executable(resolved.url):
                continue
            declared_type = _normalise_mime(_attribute(node, "type"))
            if not _embedded_document(resolved.url, declared_type, assume_unknown=True):
                continue
            references.append(
                _candidate(
                    resolved,
                    kind="document",
                    expected_content="document",
                    rule=f"{tag}-{source_attribute}",
                    ordinal=ordinal,
                    source_attribute=source_attribute,
                    node=node,
                    nearby_heading=nearby_heading,
                    declared_type=declared_type,
                )
            )
            continue

        if tag == "iframe":
            resolved = _resolve_url(document_base, _attribute(node, "src"))
            if resolved is None or _is_executable(resolved.url):
                continue
            declared_type = _normalise_mime(_attribute(node, "type"))
            if not _embedded_document(resolved.url, declared_type, assume_unknown=False):
                continue
            references.append(
                _candidate(
                    resolved,
                    kind="document",
                    expected_content="document",
                    rule="iframe-document",
                    ordinal=ordinal,
                    source_attribute="src",
                    node=node,
                    nearby_heading=nearby_heading,
                    declared_type=declared_type,
                )
            )
            continue

        if tag == "meta" and images_returned < max_images_per_page:
            key = (_attribute(node, "property") or _attribute(node, "name") or "").strip().lower()
            if key not in _OG_IMAGE_KEYS | _TWITTER_IMAGE_KEYS:
                continue
            resolved = _resolve_url(document_base, _attribute(node, "content"))
            if resolved is None or _is_executable(resolved.url):
                continue
            is_og = key in _OG_IMAGE_KEYS
            references.append(
                _candidate(
                    resolved,
                    kind="image",
                    expected_content="image",
                    rule="meta-og-image" if is_og else "meta-twitter-image",
                    ordinal=ordinal,
                    source_attribute="content",
                    node=node,
                    nearby_heading=nearby_heading,
                    alt_text=og_alt if is_og else twitter_alt,
                )
            )
            images_returned += 1

    return references


__all__ = ["REFERENCE_EXTRACTOR_VERSION", "ReferenceCandidate", "extract_references"]
