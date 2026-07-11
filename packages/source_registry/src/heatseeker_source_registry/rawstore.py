"""Content-addressed raw evidence store: data/raw/<h2>/<h4>/<sha256> (ADR-0007).

Write-once: identical content maps to the same path and is never rewritten.
"""

import gzip
import hashlib
from pathlib import Path

from heatseeker_common.settings import Settings

# Compress textual payloads at rest (HTML compresses 5-10x). The content hash is always
# of the ORIGINAL bytes; a .gz suffix on the stored path marks the encoding.
_COMPRESSIBLE_HINTS = ("text", "html", "xml", "json", "rss", "atom", "javascript", "csv")
_COMPRESS_MIN_BYTES = 512


def content_address(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _should_compress(settings: Settings, content: bytes, content_type: str | None) -> bool:
    if not settings.store_compression or len(content) < _COMPRESS_MIN_BYTES:
        return False
    lowered = (content_type or "").lower()
    return any(hint in lowered for hint in _COMPRESSIBLE_HINTS)


def store_bytes(
    settings: Settings, content: bytes, content_type: str | None = None
) -> tuple[str, str]:
    """Store content; return (relative_path, sha256-of-original). Idempotent."""
    digest = content_address(content)
    rel = f"{digest[:2]}/{digest[2:4]}/{digest}"
    if _should_compress(settings, content, content_type):
        rel += ".gz"
        payload = gzip.compress(content)
    else:
        payload = content
    target = settings.raw_dir / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.rename(target)
    return rel, digest


def read_bytes(settings: Settings, relative_path: str) -> bytes:
    path = settings.raw_dir / relative_path
    if not path.is_file() or not path.resolve().is_relative_to(settings.raw_dir.resolve()):
        raise FileNotFoundError(relative_path)
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix == ".gz" else raw


def exists(settings: Settings, relative_path: str) -> bool:
    return (settings.raw_dir / Path(relative_path)).is_file()
