"""M6 over the UI and JSON API: project workspace, connections, graph queries."""

from fastapi.testclient import TestClient
from heatseeker_api.main import create_app
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities


def _seed_orgs(engine):
    with session_scope(engine) as session:
        orgs = [
            entities.create_organisation(session, name)
            for name in ("Acme Scaffolding", "BigBuild", "SteelCo")
        ]
        return [o.id for o in orgs]


def test_project_workspace_roundtrip(engine, settings):
    acme_id, big_id, _steel_id = _seed_orgs(engine)
    client = TestClient(create_app(settings))

    created = client.post(
        "/api/projects",
        json={"name": "Hospital North Tower", "status": "active",
              "project_type_ids": ["major_project"]},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    pairs = ((acme_id, "scaffold_contractor"), (big_id, "principal_contractor"))
    for organisation_id, role in pairs:
        added = client.post(
            f"/api/projects/{project_id}/participants",
            json={"organisation_id": organisation_id, "role_type": role,
                  "status": "confirmed", "confidence": 0.8},
        )
        assert added.status_code == 200

    detail = client.get(f"/api/projects/{project_id}").json()
    assert {p["role_type"] for p in detail["participants"]} == {
        "scaffold_contractor", "principal_contractor",
    }
    # Edge confidence inspectable straight off the API.
    assert all("confidence" in p for p in detail["participants"])

    page = client.get(f"/projects/{project_id}")
    assert page.status_code == 200
    assert "Hospital North Tower" in page.text
    assert "Add participant" in page.text

    listing = client.get("/projects")
    assert "Hospital North Tower" in listing.text


def test_relationships_and_graph_queries(engine, settings):
    acme_id, big_id, steel_id = _seed_orgs(engine)
    client = TestClient(create_app(settings))

    project = client.post("/api/projects", json={"name": "Stadium Stage 2"}).json()
    pairs = ((acme_id, "scaffold_contractor"), (big_id, "principal_contractor"))
    for organisation_id, role in pairs:
        client.post(
            f"/api/projects/{project['id']}/participants",
            json={"organisation_id": organisation_id, "role_type": role, "confidence": 0.8},
        )
    edge = client.post(
        "/api/relationships",
        json={"subject_entity_id": big_id, "object_entity_id": steel_id,
              "relationship_type": "customer_of", "confidence": 0.6},
    )
    assert edge.status_code == 201
    edge_id = edge.json()["id"]

    self_edge = client.post(
        "/api/relationships",
        json={"subject_entity_id": acme_id, "object_entity_id": acme_id,
              "relationship_type": "partner_of"},
    )
    assert self_edge.status_code == 409

    # Multi-hop: acme —project— big —relationship— steel.
    hood = client.get(f"/api/graph/{acme_id}/neighbourhood", params={"depth": 2}).json()
    by_id = {n["organisation_id"]: n for n in hood["neighbours"]}
    assert set(by_id) == {big_id, steel_id}
    assert by_id[steel_id]["hops"] == 2
    assert by_id[steel_id]["path_confidence"] > 0
    assert [hop["edge"]["kind"] for hop in by_id[steel_id]["via"]] == [
        "project", "relationship",
    ]

    paths = client.get(
        "/api/graph/paths", params={"from": acme_id, "to": steel_id}
    ).json()["paths"]
    assert len(paths) == 1
    assert paths[0]["confidence"] > 0

    # History keeps dates: ending the edge leaves it visible with a valid_to.
    ended = client.post(f"/api/relationships/{edge_id}/end")
    assert ended.status_code == 200
    assert ended.json()["valid_to"]
    again = client.post(f"/api/relationships/{edge_id}/end")
    assert again.status_code == 409

    entity_page = client.get(f"/entities/{big_id}")
    assert "Connections" in entity_page.text
    assert "historical" in entity_page.text  # ended edge still shown, dated


def test_entity_page_relationship_form_and_network_explorer(engine, settings):
    acme_id, big_id, _steel_id = _seed_orgs(engine)
    client = TestClient(create_app(settings))

    response = client.post(
        "/relationships/create",
        data={"subject_entity_id": acme_id, "object_entity_id": big_id,
              "relationship_type": "supplier_to", "confidence": "0.7"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get(f"/entities/{acme_id}")
    assert "supplier_to" in page.text
    assert "Network explorer" in page.text
    assert "BigBuild" in page.text

    # Guidance checklist now tracks connections.
    dashboard = client.get("/")
    assert "Map projects &amp; relationships" in dashboard.text
