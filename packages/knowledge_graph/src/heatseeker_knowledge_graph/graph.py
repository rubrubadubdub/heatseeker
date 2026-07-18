"""Relational graph query layer (spec §23): typed edges, bounded multi-hop paths.

Every edge a traversal returns is inspectable — type, direction, confidence, validity
dates, evidence count, and (for derived co-participation edges) the project it came
from. Path confidence is the product of edge confidences, so weak links are visible
rather than laundered.
"""

from dataclasses import dataclass, field
from datetime import datetime

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution.models import Organisation
from heatseeker_entity_resolution.resolution import canonical_id, merge_group
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from heatseeker_knowledge_graph.evidence import validate_confidence, validate_evidence_ids
from heatseeker_knowledge_graph.models import (
    ParticipationStatus,
    ProjectParticipation,
    Relationship,
    RelationshipStatus,
)

MAX_PATH_DEPTH = 4
_TRAVERSABLE_PARTICIPATION = (
    ParticipationStatus.PROBABLE,
    ParticipationStatus.CONFIRMED,
)


class GraphError(ValueError):
    """An edge operation that would violate graph invariants."""


@dataclass(slots=True)
class Edge:
    """One traversable connection, fully inspectable (§23.2)."""

    kind: str  # "relationship" | "project"
    label: str  # relationship_type or co-participation description
    direction: str  # "out" | "in" | "both" (project edges are undirected)
    other_id: str
    confidence: float
    evidence_count: int
    ref_id: str  # relationship id or project id
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    detail: dict = field(default_factory=dict)


@dataclass(slots=True)
class PathHop:
    edge: Edge
    node_id: str


def add_relationship(
    session: Session,
    subject_entity_id: str,
    object_entity_id: str,
    relationship_type: str,
    *,
    confidence: float = 0.5,
    valid_from: datetime | None = None,
    evidence_ids: list[str] | None = None,
    created_by: str = "user",
) -> Relationship:
    """Create (or strengthen) the open edge of this type between two organisations."""
    relationship_type = relationship_type.strip().lower()
    if not relationship_type:
        raise GraphError("relationship_type must not be blank")
    subject_id = canonical_id(session, subject_entity_id)
    object_id = canonical_id(session, object_entity_id)
    if subject_id == object_id:
        raise GraphError(
            "subject and object resolve to the same organisation — a relationship "
            "needs two distinct entities"
        )
    if session.get(Organisation, subject_id) is None:
        raise LookupError(f"organisation not found: {subject_id}")
    if session.get(Organisation, object_id) is None:
        raise LookupError(f"organisation not found: {object_id}")
    if valid_from is not None and valid_from.tzinfo is None:
        raise GraphError("valid_from must include a timezone")
    evidence = validate_evidence_ids(session, evidence_ids)

    open_edge = session.execute(
        select(Relationship).where(
            Relationship.subject_entity_id == subject_id,
            Relationship.object_entity_id == object_id,
            Relationship.relationship_type == relationship_type,
            Relationship.status == RelationshipStatus.ACTIVE,
        )
    ).scalar_one_or_none()
    if open_edge is not None:
        combined_evidence = sorted(set(open_edge.evidence_ids) | set(evidence))
        validated_confidence = validate_confidence(
            confidence, has_evidence=bool(combined_evidence)
        )
        open_edge.evidence_ids = combined_evidence
        open_edge.confidence = max(open_edge.confidence, validated_confidence)
        open_edge.updated_at = utc_now()
        session.flush()
        audit.record(
            session,
            created_by,
            "relationship.updated",
            "relationship",
            open_edge.id,
            {"confidence": open_edge.confidence, "evidence_count": len(combined_evidence)},
        )
        return open_edge

    edge = Relationship(
        subject_entity_id=subject_id,
        object_entity_id=object_id,
        relationship_type=relationship_type,
        confidence=validate_confidence(confidence, has_evidence=bool(evidence)),
        valid_from=valid_from or utc_now(),
        evidence_ids=evidence,
        created_by=created_by,
    )
    session.add(edge)
    session.flush()
    audit.record(
        session,
        created_by,
        "relationship.created",
        "relationship",
        edge.id,
        {"relationship_type": relationship_type, "confidence": edge.confidence},
    )
    return edge


def _close(
    session: Session,
    relationship_id: str,
    status: str,
    when: datetime | None,
    actor: str,
):
    edge = session.get(Relationship, relationship_id)
    if edge is None:
        raise LookupError(f"relationship not found: {relationship_id}")
    if edge.status != RelationshipStatus.ACTIVE:
        raise GraphError(f"relationship is already {edge.status} — history is immutable")
    valid_to = when or utc_now()
    if valid_to.tzinfo is None:
        raise GraphError("valid_to must include a timezone")
    if edge.valid_from is not None and valid_to < edge.valid_from:
        raise GraphError("valid_to must not precede valid_from")
    edge.status = status
    edge.valid_to = valid_to
    edge.updated_at = utc_now()
    session.flush()
    audit.record(
        session,
        actor,
        "relationship.closed",
        "relationship",
        edge.id,
        {"status": status, "valid_to": edge.valid_to.isoformat()},
    )
    return edge


def end_relationship(
    session: Session,
    relationship_id: str,
    *,
    valid_to: datetime | None = None,
    actor: str = "user",
) -> Relationship:
    """The relationship genuinely ended — dates retained (acceptance: history)."""
    return _close(session, relationship_id, RelationshipStatus.HISTORICAL, valid_to, actor)


def retract_relationship(
    session: Session, relationship_id: str, *, actor: str = "user"
) -> Relationship:
    """The edge was judged wrong — kept for audit, excluded from traversal."""
    return _close(session, relationship_id, RelationshipStatus.RETRACTED, None, actor)


def _group_ids(session: Session, entity_id: str) -> list[str]:
    return [organisation.id for organisation in merge_group(session, entity_id)]


def edges_for(
    session: Session, entity_id: str, *, include_historical: bool = False
) -> list[Edge]:
    """All inspectable edges for one organisation (merge group aware)."""
    group_ids = _group_ids(session, entity_id)
    statuses = [RelationshipStatus.ACTIVE]
    if include_historical:
        statuses.append(RelationshipStatus.HISTORICAL)

    edges: list[Edge] = []
    rows = session.execute(
        select(Relationship).where(
            or_(
                Relationship.subject_entity_id.in_(group_ids),
                Relationship.object_entity_id.in_(group_ids),
            ),
            Relationship.status.in_(statuses),
        )
    ).scalars()
    for row in rows:
        outbound = row.subject_entity_id in group_ids
        other = row.object_entity_id if outbound else row.subject_entity_id
        edges.append(
            Edge(
                kind="relationship",
                label=row.relationship_type,
                direction="out" if outbound else "in",
                other_id=canonical_id(session, other),
                confidence=row.confidence,
                evidence_count=len(row.evidence_ids),
                ref_id=row.id,
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                detail={"status": row.status},
            )
        )

    mine = session.execute(
        select(ProjectParticipation)
        .options(selectinload(ProjectParticipation.project))
        .where(
            ProjectParticipation.organisation_id.in_(group_ids),
            ProjectParticipation.status.in_(_TRAVERSABLE_PARTICIPATION),
        )
    ).scalars().all()
    project_ids = {participation.project_id for participation in mine}
    peers = (
        session.scalars(
            select(ProjectParticipation).where(
                ProjectParticipation.project_id.in_(project_ids),
                ProjectParticipation.organisation_id.not_in(group_ids),
                ProjectParticipation.status.in_(_TRAVERSABLE_PARTICIPATION),
            )
        ).all()
        if project_ids
        else []
    )
    peers_by_project: dict[str, list[ProjectParticipation]] = {}
    for peer in peers:
        peers_by_project.setdefault(peer.project_id, []).append(peer)

    project_edges: dict[tuple[str, str], dict] = {}
    for participation in mine:
        for peer in peers_by_project.get(participation.project_id, []):
            other_id = canonical_id(session, peer.organisation_id)
            key = (participation.project_id, other_id)
            candidate_confidence = round(
                min(participation.confidence, peer.confidence), 3
            )
            aggregate = project_edges.setdefault(
                key,
                {
                    "project_name": participation.project.name,
                    "confidence": candidate_confidence,
                    "evidence_ids": set(),
                    "my_roles": set(),
                    "their_roles": set(),
                },
            )
            aggregate["confidence"] = max(aggregate["confidence"], candidate_confidence)
            aggregate["evidence_ids"].update(participation.evidence_ids)
            aggregate["evidence_ids"].update(peer.evidence_ids)
            aggregate["my_roles"].add(participation.role_type)
            aggregate["their_roles"].add(peer.role_type)
    for (project_id, other_id), aggregate in project_edges.items():
        my_roles = sorted(aggregate["my_roles"])
        their_roles = sorted(aggregate["their_roles"])
        detail = {
            "my_role": my_roles[0] if len(my_roles) == 1 else ", ".join(my_roles),
            "their_role": (
                their_roles[0] if len(their_roles) == 1 else ", ".join(their_roles)
            ),
        }
        edges.append(
            Edge(
                kind="project",
                label=f"co-participant on {aggregate['project_name']}",
                direction="both",
                other_id=other_id,
                confidence=aggregate["confidence"],
                evidence_count=len(aggregate["evidence_ids"]),
                ref_id=project_id,
                detail=detail,
            )
        )
    return edges


@dataclass(slots=True)
class Neighbour:
    organisation: Organisation
    hops: int
    best_confidence: float
    via: list[PathHop]


def neighbourhood(
    session: Session,
    entity_id: str,
    *,
    depth: int = 2,
    min_confidence: float = 0.0,
    limit: int = 100,
) -> list[Neighbour]:
    """BFS out to `depth` hops; each neighbour keeps its best (most confident) path."""
    depth = max(1, min(depth, MAX_PATH_DEPTH))
    start = canonical_id(session, entity_id)
    best: dict[str, Neighbour] = {}
    frontier: list[tuple[str, float, list[PathHop]]] = [(start, 1.0, [])]
    visited_at_hop = {start: 0}

    for hop in range(1, depth + 1):
        next_frontier: list[tuple[str, float, list[PathHop]]] = []
        for node_id, path_confidence, path in frontier:
            for edge in edges_for(session, node_id):
                if edge.confidence < min_confidence:
                    continue
                other = edge.other_id
                if other == start:
                    continue
                new_confidence = round(path_confidence * edge.confidence, 3)
                new_path = [*path, PathHop(edge=edge, node_id=other)]
                known = best.get(other)
                if known is None or new_confidence > known.best_confidence:
                    organisation = session.get(Organisation, other)
                    best[other] = Neighbour(
                        organisation=organisation,
                        hops=hop,
                        best_confidence=new_confidence,
                        via=new_path,
                    )
                if visited_at_hop.get(other, depth + 1) > hop:
                    visited_at_hop[other] = hop
                    next_frontier.append((other, new_confidence, new_path))
        frontier = next_frontier
        if len(best) >= limit:
            break

    ordered = sorted(best.values(), key=lambda n: (n.hops, -n.best_confidence))
    return ordered[:limit]


def find_paths(
    session: Session,
    from_entity_id: str,
    to_entity_id: str,
    *,
    max_depth: int = MAX_PATH_DEPTH,
    limit: int = 5,
) -> list[list[PathHop]]:
    """Shortest paths between two organisations, most confident first."""
    max_depth = max(1, min(max_depth, MAX_PATH_DEPTH))
    start = canonical_id(session, from_entity_id)
    target = canonical_id(session, to_entity_id)
    if start == target:
        return []

    found: list[list[PathHop]] = []
    frontier: list[list[PathHop]] = [[]]
    shortest: int | None = None
    seen_depth = {start: 0}

    for hop in range(1, max_depth + 1):
        if shortest is not None and hop > shortest:
            break
        next_frontier: list[list[PathHop]] = []
        for path in frontier:
            node_id = path[-1].node_id if path else start
            for edge in edges_for(session, node_id):
                other = edge.other_id
                if any(h.node_id == other for h in path) or other == start:
                    continue
                new_path = [*path, PathHop(edge=edge, node_id=other)]
                if other == target:
                    found.append(new_path)
                    shortest = hop
                elif shortest is None and seen_depth.get(other, max_depth + 1) >= hop:
                    seen_depth[other] = hop
                    next_frontier.append(new_path)
        frontier = next_frontier

    def _path_confidence(path: list[PathHop]) -> float:
        product = 1.0
        for hop_item in path:
            product *= hop_item.edge.confidence
        return product

    found.sort(key=_path_confidence, reverse=True)
    return found[:limit]


def path_confidence(path: list[PathHop]) -> float:
    product = 1.0
    for hop in path:
        product *= hop.edge.confidence
    return round(product, 3)
