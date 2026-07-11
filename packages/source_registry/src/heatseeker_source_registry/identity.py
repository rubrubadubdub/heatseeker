"""Canonical source identities and conservative URL normalisation.

Identity aliases let multiple industry packs converge on one source definition without
merging sources by a fuzzy name or by domain alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from heatseeker_source_registry.models import SourceDefinition, SourceIdentity


class SourceIdentityConflict(ValueError):
    """Aliases supplied for one source already point at different canonical sources."""


@dataclass(frozen=True, slots=True)
class IdentitySpec:
    identity_type: str
    identity_value: str
    normalised_value: str


def _validate_identity(identity: IdentitySpec) -> IdentitySpec:
    identity_type = identity.identity_type.strip().lower()
    identity_value = identity.identity_value.strip()
    normalised_value = identity.normalised_value.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", identity_type):
        raise ValueError(f"invalid source identity type: {identity.identity_type!r}")
    if not identity_value or len(identity_value) > 2000:
        raise ValueError("source identity value must contain 1-2000 characters")
    if not normalised_value or len(normalised_value) > 2000:
        raise ValueError("normalised source identity must contain 1-2000 characters")
    return IdentitySpec(identity_type, identity_value, normalised_value)


def canonicalise_url(value: str) -> str:
    """Return a stable, conservative identity for an absolute HTTP(S) URL."""
    raw = value.strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("source URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError("source URL must not contain credentials")
    scheme = parts.scheme.lower()
    hostname = parts.hostname.encode("idna").decode("ascii").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, hostname, path, query, ""))


def pack_seed_identity(pack_id: str, seed_key: str) -> IdentitySpec:
    value = f"{pack_id.strip().lower()}:{seed_key.strip().lower()}"
    return IdentitySpec("pack_seed", value, value)


def shared_source_identity(source_key: str) -> IdentitySpec:
    value = source_key.strip().lower()
    return IdentitySpec("source_key", value, value)


def url_identity(url: str) -> IdentitySpec:
    return IdentitySpec("url", url.strip(), canonicalise_url(url))


def resolve_identities(
    session: Session, identities: Iterable[IdentitySpec]
) -> SourceDefinition | None:
    """Resolve aliases and fail closed when they disagree."""
    identities = tuple(_validate_identity(identity) for identity in identities)
    if not identities:
        return None
    clauses = [
        (SourceIdentity.identity_type == identity.identity_type)
        & (SourceIdentity.normalised_value == identity.normalised_value)
        for identity in identities
    ]
    rows = list(session.scalars(select(SourceIdentity).where(or_(*clauses))))
    source_ids = {row.source_definition_id for row in rows}
    if len(source_ids) > 1:
        aliases = ", ".join(
            f"{identity.identity_type}:{identity.normalised_value}" for identity in identities
        )
        raise SourceIdentityConflict(f"source identities resolve to different sources: {aliases}")
    return session.get(SourceDefinition, source_ids.pop()) if source_ids else None


def attach_identity(
    session: Session,
    source: SourceDefinition,
    identity: IdentitySpec,
    *,
    origin: str,
    is_primary: bool = False,
) -> tuple[SourceIdentity, bool]:
    """Attach one globally unique alias; return ``(row, created)``."""
    identity = _validate_identity(identity)
    existing = session.scalars(
        select(SourceIdentity).where(
            SourceIdentity.identity_type == identity.identity_type,
            SourceIdentity.normalised_value == identity.normalised_value,
        )
    ).first()
    if existing is not None:
        if existing.source_definition_id != source.id:
            raise SourceIdentityConflict(
                f"{identity.identity_type}:{identity.normalised_value} belongs to another source"
            )
        return existing, False
    if is_primary:
        is_primary = (
            session.scalars(
                select(SourceIdentity.id).where(
                    SourceIdentity.source_definition_id == source.id,
                    SourceIdentity.is_primary.is_(True),
                )
            ).first()
            is None
        )
    row = SourceIdentity(
        source_definition_id=source.id,
        identity_type=identity.identity_type,
        identity_value=identity.identity_value,
        normalised_value=identity.normalised_value,
        is_primary=is_primary,
        origin=origin,
    )
    session.add(row)
    session.flush()
    return row, True
