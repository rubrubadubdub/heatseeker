"""Named geography regions as data (ADR-0012).

The geo_region table is the source of truth; this module seeds it from the builtin
defaults, loads it into the in-process registry that all geography matching and
validation consults, and applies edits. Deleting builtins is refused (scopes and
habit reference them); editing them is fine — incomplete membership is a data fix,
not a release.
"""

import logging

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    KNOWN_CODES,
    builtin_macro_regions,
    set_macro_regions,
    validate_region_definition,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_source_registry.models import GeoRegion, ResearchScope, SourceCoverageTarget
from heatseeker_source_registry.targeting import REGION_DIMENSION

logger = logging.getLogger("heatseeker.regions")


def ensure_default_regions(session: Session) -> None:
    """Insert builtin regions missing from the table; never overwrite user edits.

    Idempotent, and additive over releases: a new builtin shipped in code appears on
    the next boot without touching existing rows.
    """
    existing = set(session.scalars(select(GeoRegion.code)))
    for code, members in builtin_macro_regions().items():
        if code == "GLOBAL" or code in existing:
            continue
        session.add(
            GeoRegion(
                code=code,
                name=KNOWN_CODES.get(code, code),
                member_codes=sorted(members),
                is_builtin=True,
            )
        )


def load_regions(session: Session) -> None:
    """Replace the in-process registry with the database state (seeding first)."""
    ensure_default_regions(session)
    rows = list(session.scalars(select(GeoRegion)))
    set_macro_regions(
        {row.code: row.member_codes for row in rows},
        names={row.code: row.name for row in rows},
    )


def load_regions_if_available(engine) -> None:
    """Best-effort load at process startup: an unmigrated database (fresh checkout,
    pre-migrate health probe) keeps the builtin defaults instead of failing boot."""
    from heatseeker_common.db import session_scope

    try:
        with session_scope(engine) as session:
            load_regions(session)
    except Exception:  # startup must not die on a missing table
        logger.warning("geo_region table unavailable; using builtin region defaults")


def upsert_region(
    session: Session, code_raw: str, name: str, members_raw: list[str], actor: str = "user"
) -> GeoRegion:
    """Create or update a named region, then refresh the live registry."""
    code, members = validate_region_definition(code_raw, members_raw)
    display = name.strip() or KNOWN_CODES.get(code, code)
    row = session.scalars(select(GeoRegion).where(GeoRegion.code == code)).first()
    action = "region.updated" if row else "region.created"
    if row is None:
        row = GeoRegion(code=code, name=display, member_codes=members)
        session.add(row)
    else:
        row.name = display
        row.member_codes = members
        row.updated_at = utc_now()
    session.flush()
    audit.record(session, actor, action, "geo_region", row.id, {"code": code, "members": members})
    load_regions(session)
    return row


def region_references(session: Session, code: str) -> list[str]:
    """Human-readable references that should block deletion of a region."""
    refs: list[str] = []
    for scope in session.scalars(select(ResearchScope)):
        codes = set(scope.geo_codes or []) | set(getattr(scope, "exclude_codes", None) or [])
        if code in codes:
            refs.append(f"scope '{scope.name}'")
    target_count = len(
        session.scalars(
            select(SourceCoverageTarget.id).where(
                SourceCoverageTarget.dimension == REGION_DIMENSION,
                SourceCoverageTarget.target_key == code,
            )
        ).all()
    )
    if target_count:
        refs.append(f"{target_count} source coverage target(s)")
    return refs


def delete_region(session: Session, code_raw: str, actor: str = "user") -> None:
    """Delete a custom region. Builtins and referenced regions are refused."""
    code = code_raw.strip().upper()
    row = session.scalars(select(GeoRegion).where(GeoRegion.code == code)).first()
    if row is None:
        raise ValueError(f"no region named {code!r}")
    if row.is_builtin:
        raise ValueError(f"{code} is a builtin region; edit its members instead of deleting")
    refs = region_references(session, code)
    if refs:
        raise ValueError(f"{code} is referenced by {', '.join(refs)}; remove those first")
    session.delete(row)
    audit.record(session, actor, "region.deleted", "geo_region", row.id, {"code": code})
    load_regions(session)
