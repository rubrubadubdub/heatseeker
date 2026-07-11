"""Register loaded packs in the DB. Version/hash history lands in the audit trail."""

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from sqlalchemy.orm import Session

from heatseeker_industry_packs.loader import LoadedPack
from heatseeker_industry_packs.models import PackRegistration


def register_pack(session: Session, pack: LoadedPack, actor: str = "system") -> PackRegistration:
    existing = session.get(PackRegistration, pack.pack_id)
    if existing is None:
        registration = PackRegistration(
            pack_id=pack.pack_id,
            name=pack.manifest.name,
            version=pack.version,
            content_hash=pack.content_hash,
        )
        session.add(registration)
        audit.record(
            session,
            actor,
            "pack.loaded",
            "industry_pack",
            pack.pack_id,
            {"version": pack.version, "content_hash": pack.content_hash},
        )
        return registration

    changed = (existing.version, existing.content_hash) != (pack.version, pack.content_hash)
    if changed:
        audit.record(
            session,
            actor,
            "pack.updated",
            "industry_pack",
            pack.pack_id,
            {
                "from_version": existing.version,
                "to_version": pack.version,
                "from_hash": existing.content_hash,
                "to_hash": pack.content_hash,
            },
        )
    existing.name = pack.manifest.name
    existing.version = pack.version
    existing.content_hash = pack.content_hash
    existing.loaded_at = utc_now()
    return existing
