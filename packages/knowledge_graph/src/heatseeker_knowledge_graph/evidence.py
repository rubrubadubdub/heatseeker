"""Evidence-reference validation shared by M6 project and relationship writes."""

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def validate_evidence_ids(session: Session, evidence_ids: list[str] | None) -> list[str]:
    """Return stable, deduplicated references after proving every record exists.

    M6 evidence may point to the immutable source document or to a more precise M5
    observation. Arbitrary labels are rejected so a displayed evidence count cannot
    imply provenance that the application cannot open.
    """

    cleaned = sorted({str(value).strip() for value in evidence_ids or [] if str(value).strip()})
    if not cleaned:
        return []
    found: set[str] = set()
    for table in ("source_document", "observation"):
        statement = text(f"SELECT id FROM {table} WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)
        )
        found.update(session.execute(statement, {"ids": cleaned}).scalars())
    missing = sorted(set(cleaned) - found)
    if missing:
        preview = ", ".join(missing[:3])
        raise ValueError(f"unknown evidence reference(s): {preview}")
    return cleaned


def validate_confidence(confidence: float, *, has_evidence: bool) -> float:
    value = float(confidence)
    if not 0.0 <= value <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if not has_evidence and value > 0.5:
        raise ValueError("confidence above 0.5 requires evidence")
    return value
