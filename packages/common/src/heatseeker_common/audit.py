"""Append-only audit trail helper (spec §31.3)."""

from sqlalchemy.orm import Session

from heatseeker_common.models import AuditLog


def record(
    session: Session,
    actor: str,
    action: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        detail=detail,
    )
    session.add(entry)
    return entry
