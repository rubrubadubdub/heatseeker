"""Distillation: raw HTML/XML → clean, token-lean text under data/processed/.

The conservative-token output pipe: AI tasks and parsers read the distilled text
(~5-20x smaller than raw HTML), never the raw bytes, unless they specifically need
markup. Raw evidence stays immutable and complete; distillation is derived data and
can always be regenerated (versioned via DISTILLER_VERSION).
"""

import gzip
import hashlib
import re

from heatseeker_common.settings import Settings
from selectolax.parser import HTMLParser

from heatseeker_source_registry.models import SourceDocument

DISTILLER_VERSION = "distill/0.1"

_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "canvas",
    "template",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "button",
)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")

_TEXTUAL_HINTS = ("html", "xml", "text", "json", "rss", "atom")


def is_distillable(content_type: str | None) -> bool:
    lowered = (content_type or "").lower()
    return any(hint in lowered for hint in _TEXTUAL_HINTS)


def html_to_text(raw: bytes) -> str:
    """Strip boilerplate and markup; keep readable text with title/heading structure."""
    tree = HTMLParser(raw)
    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()
    title = tree.css_first("title")
    body = tree.body or tree.root
    text = body.text(separator="\n") if body is not None else ""
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _NL_RE.sub("\n\n", text).strip()
    if title and title.text(strip=True):
        text = f"# {title.text(strip=True)}\n\n{text}"
    return text


def distilled_rel_path(content_hash: str) -> str:
    version = hashlib.sha256(DISTILLER_VERSION.encode()).hexdigest()[:12]
    return f"{content_hash[:2]}/{content_hash[2:4]}/{content_hash}/distill-{version}.txt.gz"


def distill_document(settings: Settings, document: SourceDocument, raw: bytes) -> bool:
    """Produce and store distilled text for a document. Returns True when distilled."""
    if not is_distillable(document.content_type):
        return False
    lowered = (document.content_type or "").lower()
    if "html" in lowered or "xml" in lowered:
        text = html_to_text(raw)
    else:  # already-plain text/json: normalise whitespace only
        text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return False

    rel = distilled_rel_path(document.content_hash)
    target = settings.processed_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(gzip.compress(text.encode("utf-8")))
    document.distilled_path = rel
    document.distilled_chars = len(text)
    document.parser_version = DISTILLER_VERSION
    return True


def read_distilled(settings: Settings, document: SourceDocument) -> str | None:
    if not document.distilled_path:
        return None
    root = settings.processed_dir.resolve()
    path = (root / document.distilled_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return gzip.decompress(path.read_bytes()).decode("utf-8")
