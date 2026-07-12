"""Canonical identity, reversible merge/split, and queue decisions (spec §14.4-§14.5).

A merge only ever sets `merged_into_id` + status on the absorbed record and writes an
`entity_merge` audit row. Child rows (identifiers, domains, contacts, units) never move,
so reversal restores the exact prior state and branch/parent structure is never
flattened.
"""

from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_entity_resolution.models import (
    CandidateResolution,
    EntityMatchCandidate,
    EntityMerge,
    MatchState,
    Organisation,
    OrganisationStatus,
)


class ResolutionError(ValueError):
    """A merge/split/decision that would violate resolution invariants."""


def canonical_id(session: Session, organisation_id: str) -> str:
    """Follow the merged_into chain to the live representative record."""
    seen: set[str] = set()
    current = organisation_id
    while True:
        if current in seen:
            raise ResolutionError(f"merge chain cycle at organisation {current}")
        seen.add(current)
        organisation = session.get(Organisation, current)
        if organisation is None:
            raise LookupError(f"organisation not found: {current}")
        if organisation.merged_into_id is None:
            return organisation.id
        current = organisation.merged_into_id


def merge_group(session: Session, organisation_id: str) -> list[Organisation]:
    """Canonical record first, then every record absorbed into it (transitively)."""
    root = canonical_id(session, organisation_id)
    merged = (
        session.execute(
            select(Organisation).where(Organisation.merged_into_id.is_not(None))
        )
        .scalars()
        .all()
    )
    children: dict[str, list[Organisation]] = {}
    for organisation in merged:
        children.setdefault(organisation.merged_into_id, []).append(organisation)

    group = [session.get(Organisation, root)]
    queue = [root]
    while queue:
        for child in children.get(queue.pop(), []):
            group.append(child)
            queue.append(child.id)
    return group


def group_profile(session: Session, organisation_id: str) -> dict:
    """Aggregated view across the merge group, attributing every row to its origin."""
    group = merge_group(session, organisation_id)
    canonical = group[0]

    def _attributed(items_of):
        rows = []
        for organisation in group:
            for item in items_of(organisation):
                rows.append({"origin": organisation, "item": item})
        return rows

    return {
        "canonical": canonical,
        "group": group,
        "identifiers": _attributed(lambda o: o.identifiers),
        "domains": _attributed(lambda o: o.domains),
        "contact_points": _attributed(lambda o: o.contact_points),
        "units": _attributed(lambda o: o.units),
    }


def _candidate_for_pair(
    session: Session, org_id_1: str, org_id_2: str
) -> EntityMatchCandidate | None:
    a_id, b_id = sorted((org_id_1, org_id_2))
    return session.execute(
        select(EntityMatchCandidate).where(
            EntityMatchCandidate.organisation_a_id == a_id,
            EntityMatchCandidate.organisation_b_id == b_id,
        )
    ).scalar_one_or_none()


def _ancestor_ids(session: Session, organisation: Organisation) -> set[str]:
    """Return every represented parent, tolerating malformed legacy cycles."""
    ancestors: set[str] = set()
    pending = [organisation.parent_organisation_id, organisation.ultimate_parent_id]
    while pending:
        parent_id = pending.pop()
        if parent_id is None or parent_id in ancestors:
            continue
        ancestors.add(parent_id)
        parent = session.get(Organisation, parent_id)
        if parent is not None:
            pending.extend([parent.parent_organisation_id, parent.ultimate_parent_id])
    return ancestors


def perform_merge(
    session: Session,
    survivor_id: str,
    absorbed_id: str,
    *,
    rationale: str,
    performed_by: str = "user",
    candidate_id: str | None = None,
) -> EntityMerge:
    if not rationale.strip():
        raise ResolutionError("a merge requires a rationale (spec §14.4)")
    survivor = session.get(Organisation, canonical_id(session, survivor_id))
    absorbed = session.get(Organisation, absorbed_id)
    if absorbed is None:
        raise LookupError(f"organisation not found: {absorbed_id}")
    if absorbed.merged_into_id is not None:
        raise ResolutionError("that record is already merged — reverse the existing merge first")
    if survivor.id == absorbed.id:
        raise ResolutionError("cannot merge an organisation into itself")
    if absorbed.id in _ancestor_ids(session, survivor) or survivor.id in _ancestor_ids(
        session, absorbed
    ):
        raise ResolutionError(
            "parent and subsidiary are related organisations, not duplicates — "
            "record the relationship instead of merging (spec §14.1, §35 M4)"
        )

    candidate = None
    if candidate_id is not None:
        candidate = session.get(EntityMatchCandidate, candidate_id)
        if candidate is None:
            raise LookupError(f"match candidate not found: {candidate_id}")
        candidate_pair = {candidate.organisation_a_id, candidate.organisation_b_id}
        if candidate_pair != {survivor.id, absorbed.id}:
            raise ResolutionError("match candidate does not describe the merge pair")
        if candidate.resolution is not None:
            raise ResolutionError("match candidate is already resolved")
    else:
        possible_candidate = _candidate_for_pair(session, survivor.id, absorbed.id)
        if possible_candidate is not None:
            if possible_candidate.resolution is not None:
                raise ResolutionError(
                    "match candidate already has a human decision; do not bypass it with "
                    "a direct merge"
                )
            candidate = possible_candidate

    merge = EntityMerge(
        survivor_id=survivor.id,
        absorbed_id=absorbed.id,
        candidate_id=candidate.id if candidate else None,
        rationale=rationale.strip(),
        signals_snapshot=list(candidate.signals) if candidate else [],
        absorbed_prior_status=absorbed.status,
        candidate_prior_match_state=candidate.match_state if candidate else None,
        candidate_prior_resolution=candidate.resolution if candidate else None,
        candidate_prior_resolved_by=candidate.resolved_by if candidate else None,
        candidate_prior_resolved_at=candidate.resolved_at if candidate else None,
        candidate_prior_notes=candidate.notes if candidate else None,
        candidate_prior_updated_at=candidate.updated_at if candidate else None,
        performed_by=performed_by,
    )
    session.add(merge)
    absorbed.status = OrganisationStatus.MERGED
    absorbed.merged_into_id = survivor.id
    if candidate is not None:
        candidate.resolution = CandidateResolution.MERGED
        candidate.resolved_by = performed_by
        candidate.resolved_at = utc_now()
        candidate.updated_at = utc_now()
    session.flush()
    return merge


def reverse_merge(
    session: Session, merge_id: str, *, reason: str, performed_by: str = "user"
) -> EntityMerge:
    """Split: restore the absorbed record exactly as it was before the merge."""
    merge = session.get(EntityMerge, merge_id)
    if merge is None:
        raise LookupError(f"merge not found: {merge_id}")
    if merge.reversed_at is not None:
        raise ResolutionError("this merge is already reversed")
    if not reason.strip():
        raise ResolutionError("a reversal requires a reason")
    absorbed = merge.absorbed
    if absorbed.merged_into_id != merge.survivor_id:
        raise ResolutionError(
            "absorbed record no longer points at this survivor — resolve manually"
        )

    absorbed.merged_into_id = None
    absorbed.status = merge.absorbed_prior_status
    merge.reversed_at = utc_now()
    merge.reversed_by = performed_by
    merge.reversal_reason = reason.strip()

    candidate = (
        session.get(EntityMatchCandidate, merge.candidate_id) if merge.candidate_id else None
    )
    if candidate is not None and candidate.resolution == CandidateResolution.MERGED:
        if merge.candidate_prior_match_state is not None:
            candidate.match_state = merge.candidate_prior_match_state
            candidate.resolution = merge.candidate_prior_resolution
            candidate.resolved_by = merge.candidate_prior_resolved_by
            candidate.resolved_at = merge.candidate_prior_resolved_at
            candidate.notes = merge.candidate_prior_notes
            candidate.updated_at = merge.candidate_prior_updated_at or utc_now()
        else:
            # Backward-compatible reversal for merges created before migration 0014.
            candidate.resolution = None
            candidate.resolved_by = None
            candidate.resolved_at = None
            candidate.updated_at = utc_now()
    session.flush()
    return merge


def list_queue(
    session: Session, *, include_resolved: bool = False, limit: int = 200
) -> list[EntityMatchCandidate]:
    """Review queue, most decision-worthy first (spec §14.5, Phase-1 subset)."""
    stmt = select(EntityMatchCandidate).order_by(
        EntityMatchCandidate.priority_score.desc(),
        EntityMatchCandidate.score.desc(),
        EntityMatchCandidate.conflict_count.desc(),
        EntityMatchCandidate.created_at,
    )
    if not include_resolved:
        stmt = stmt.where(EntityMatchCandidate.resolution.is_(None))
    return list(session.execute(stmt.limit(limit)).scalars())


def record_decision(
    session: Session,
    candidate_id: str,
    decision: str,
    *,
    resolved_by: str = "user",
    notes: str | None = None,
    survivor_id: str | None = None,
) -> EntityMatchCandidate:
    """Apply a human decision to a queue entry: merge | related | distinct."""
    candidate = session.get(EntityMatchCandidate, candidate_id)
    if candidate is None:
        raise LookupError(f"match candidate not found: {candidate_id}")
    if candidate.resolution is not None:
        raise ResolutionError("this candidate is already resolved")

    if decision == "merge":
        pair = {candidate.organisation_a_id, candidate.organisation_b_id}
        if survivor_id not in pair:
            raise ResolutionError("survivor_id must be one of the candidate pair")
        absorbed_id = next(iter(pair - {survivor_id}))
        perform_merge(
            session,
            survivor_id,
            absorbed_id,
            rationale=notes or f"resolution queue decision (score {candidate.score})",
            performed_by=resolved_by,
            candidate_id=candidate.id,
        )
    elif decision == "related":
        candidate.match_state = MatchState.RELATED_BUT_DISTINCT
        candidate.resolution = CandidateResolution.RELATED
    elif decision == "distinct":
        candidate.match_state = MatchState.CONFIRMED_DISTINCT
        candidate.resolution = CandidateResolution.DISTINCT
    else:
        raise ResolutionError(f"unknown decision: {decision!r}")

    if decision in {"related", "distinct"}:
        candidate.resolved_by = resolved_by
        candidate.resolved_at = utc_now()
        candidate.updated_at = utc_now()
    if notes:
        candidate.notes = notes
    session.flush()
    return candidate
