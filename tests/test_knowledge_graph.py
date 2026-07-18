"""Knowledge graph core: edges, history, canonicalisation, multi-hop (M6, spec §23)."""

import pytest
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.resolution import perform_merge
from heatseeker_knowledge_graph import graph, projects
from heatseeker_knowledge_graph.models import (
    ParticipationStatus,
    Relationship,
    RelationshipStatus,
)
from sqlalchemy import select
from test_intelligence_facts import make_document, make_source


def _orgs(session, *names):
    return [entities.create_organisation(session, name) for name in names]


def _evidence(session, label):
    source = make_source(session, f"Graph evidence {label}")
    return make_document(session, source, label).id


def test_relationship_lifecycle_keeps_history(engine):
    with session_scope(engine) as session:
        acme, builder = _orgs(session, "Acme Scaffolding", "BigBuild")
        evidence_1 = _evidence(session, "relationship-1")
        evidence_2 = _evidence(session, "relationship-2")
        edge = graph.add_relationship(
            session, acme.id, builder.id, "supplier_to", confidence=0.7,
            evidence_ids=[evidence_1],
        )
        edge_id = edge.id
        assert edge.status == RelationshipStatus.ACTIVE
        assert edge.valid_from is not None

        # Re-adding the same open edge strengthens it instead of duplicating.
        again = graph.add_relationship(
            session, acme.id, builder.id, "supplier_to", confidence=0.9,
            evidence_ids=[evidence_2],
        )
        assert again.id == edge_id
        assert again.confidence == 0.9
        assert again.evidence_ids == sorted([evidence_1, evidence_2])

        ended = graph.end_relationship(session, edge_id)
        assert ended.status == RelationshipStatus.HISTORICAL
        assert ended.valid_to is not None  # history keeps dates (acceptance)
        with pytest.raises(graph.GraphError):
            graph.end_relationship(session, edge_id)  # history is immutable

        # After ending, a new edge of the same type can open a new period.
        new_period = graph.add_relationship(
            session, acme.id, builder.id, "supplier_to", confidence=0.5
        )
        assert new_period.id != edge_id
        rows = list(session.execute(select(Relationship)).scalars())
        assert len(rows) == 2  # both periods retained

        with pytest.raises(graph.GraphError):
            graph.add_relationship(session, acme.id, acme.id, "supplier_to")


def test_edges_resolve_merged_organisations_to_canonical(engine):
    with session_scope(engine) as session:
        survivor, dupe, client = _orgs(session, "Acme Pty Ltd", "Acme (dupe)", "BigBuild")
        evidence_id = _evidence(session, "canonical")
        graph.add_relationship(
            session,
            dupe.id,
            client.id,
            "supplier_to",
            confidence=0.8,
            evidence_ids=[evidence_id],
        )
        perform_merge(session, survivor.id, dupe.id, rationale="same business")

        edges = graph.edges_for(session, survivor.id)
        assert len(edges) == 1  # the absorbed record's edge belongs to the survivor now
        assert edges[0].other_id == client.id

        # Writing through the absorbed record lands on the canonical too.
        edge = graph.add_relationship(session, client.id, dupe.id, "customer_of")
        assert edge.object_entity_id == survivor.id


def test_co_participation_creates_derived_inspectable_edges(engine):
    with session_scope(engine) as session:
        acme, big = _orgs(session, "Acme Scaffolding", "BigBuild")
        evidence_a = _evidence(session, "project-acme")
        evidence_b = _evidence(session, "project-big")
        project = projects.create_project(session, "Hospital North Tower", status="active")
        projects.add_participation(
            session, project.id, acme.id, "scaffold_contractor",
            status=ParticipationStatus.CONFIRMED,
            confidence=0.9,
            evidence_ids=[evidence_a],
        )
        projects.add_participation(
            session, project.id, big.id, "principal_contractor",
            status=ParticipationStatus.CONFIRMED,
            confidence=0.7,
            evidence_ids=[evidence_b],
        )
        edges = graph.edges_for(session, acme.id)
        project_edges = [e for e in edges if e.kind == "project"]
        assert len(project_edges) == 1
        edge = project_edges[0]
        assert edge.other_id == big.id
        assert edge.confidence == 0.7  # min of the two participations
        assert edge.detail == {
            "my_role": "scaffold_contractor",
            "their_role": "principal_contractor",
        }
        assert edge.ref_id == project.id  # inspectable: which project connects them

        # Retracted participation drops out of traversal but stays on the project.
        participation = project.participations[0]
        projects.set_participation_status(
            session, participation.id, ParticipationStatus.RETRACTED
        )
        assert [e for e in graph.edges_for(session, acme.id) if e.kind == "project"] == []
        assert len(projects.get_project(session, project.id).participations) == 2


def test_participation_accumulates_instead_of_duplicating(engine):
    with session_scope(engine) as session:
        (acme,) = _orgs(session, "Acme Scaffolding")
        evidence_1 = _evidence(session, "participation-1")
        evidence_2 = _evidence(session, "participation-2")
        project = projects.create_project(session, "Bridge Refit")
        first = projects.add_participation(
            session, project.id, acme.id, "scaffold_contractor", confidence=0.4,
            evidence_ids=[evidence_1],
        )
        second = projects.add_participation(
            session, project.id, acme.id, "scaffold_contractor", confidence=0.8,
            evidence_ids=[evidence_2], status=ParticipationStatus.CONFIRMED,
        )
        assert second.id == first.id
        assert second.confidence == 0.8
        assert second.evidence_ids == sorted([evidence_1, evidence_2])
        assert second.status == ParticipationStatus.CONFIRMED


def test_multi_hop_neighbourhood_and_paths(engine):
    with session_scope(engine) as session:
        acme, big, steel = _orgs(session, "Acme Scaffolding", "BigBuild", "SteelCo")
        evidence_acme = _evidence(session, "path-acme")
        evidence_big = _evidence(session, "path-big")
        evidence_relationship = _evidence(session, "path-relationship")
        # acme —(project)— big —(relationship)— steel
        project = projects.create_project(session, "Stadium Stage 2", status="active")
        projects.add_participation(
            session,
            project.id,
            acme.id,
            "scaffold_contractor",
            status=ParticipationStatus.PROBABLE,
            confidence=0.9,
            evidence_ids=[evidence_acme],
        )
        projects.add_participation(
            session,
            project.id,
            big.id,
            "principal_contractor",
            status=ParticipationStatus.PROBABLE,
            confidence=0.8,
            evidence_ids=[evidence_big],
        )
        graph.add_relationship(
            session,
            big.id,
            steel.id,
            "supplier_to",
            confidence=0.6,
            evidence_ids=[evidence_relationship],
        )

        near = graph.neighbourhood(session, acme.id, depth=1)
        assert [n.organisation.id for n in near] == [big.id]

        wide = graph.neighbourhood(session, acme.id, depth=2)
        by_id = {n.organisation.id: n for n in wide}
        assert set(by_id) == {big.id, steel.id}
        steel_neighbour = by_id[steel.id]
        assert steel_neighbour.hops == 2
        # Path confidence is the product of edge confidences — inspectable per hop.
        assert steel_neighbour.best_confidence == pytest.approx(0.8 * 0.6, abs=0.001)
        assert [hop.edge.kind for hop in steel_neighbour.via] == ["project", "relationship"]

        # min_confidence prunes the weak second hop.
        strong_only = graph.neighbourhood(session, acme.id, depth=2, min_confidence=0.7)
        assert {n.organisation.id for n in strong_only} == {big.id}

        paths = graph.find_paths(session, acme.id, steel.id)
        assert len(paths) == 1
        assert graph.path_confidence(paths[0]) == pytest.approx(0.48, abs=0.001)
        assert [hop.node_id for hop in paths[0]] == [big.id, steel.id]

        # No path once the linking relationship is retracted.
        edge_row = session.execute(select(Relationship)).scalar_one()
        graph.retract_relationship(session, edge_row.id)
        assert graph.find_paths(session, acme.id, steel.id) == []
