"""Entities & resolution over the JSON API and browser UI (M4)."""

from fastapi.testclient import TestClient
from heatseeker_api.main import create_app


def _create(client, name, **extra):
    response = client.post("/api/entities", json={"canonical_name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_entity_crud_scan_decide_reverse_roundtrip(engine, settings):
    client = TestClient(create_app(settings))

    a = _create(
        client,
        "Acme Scaffolding Pty Ltd",
        identifiers=[["abn", "51 824 753 556"]],
        domains=["https://www.acme.com.au"],
    )
    b = _create(client, "Acme Scaffold Hire", identifiers=[["abn", "51824753556"]])
    assert a["identifiers"][0]["value"] == "51 824 753 556"
    assert a["domains"] == ["acme.com.au"]

    listed = client.get("/api/entities").json()["organisations"]
    assert len(listed) == 2

    summary = client.post("/api/resolution/scan").json()
    assert summary["candidates_created"] == 1

    queue = client.get("/api/resolution/queue").json()["candidates"]
    assert len(queue) == 1
    assert queue[0]["match_state"] == "exact"
    assert any(s["signal"] == "shared_identifier" for s in queue[0]["signals"])

    decided = client.post(
        f"/api/resolution/{queue[0]['id']}/decide",
        json={"decision": "merge", "survivor_id": a["id"], "notes": "same ABN"},
    )
    assert decided.status_code == 200
    assert decided.json()["resolution"] == "merged"

    # Absorbed record preserved; group profile aggregates with attribution.
    profile = client.get(f"/api/entities/{b['id']}").json()
    assert profile["canonical"]["id"] == a["id"]
    assert {o["id"] for o in profile["group"]} == {a["id"], b["id"]}
    assert len(profile["identifiers"]) == 2  # one per origin record

    # Merged records drop out of the default listing but stay retrievable.
    assert len(client.get("/api/entities").json()["organisations"]) == 1
    assert (
        len(client.get("/api/entities", params={"include_merged": True}).json()["organisations"])
        == 2
    )

    # Reverse the merge via the API; both records live again.
    merged_b = client.get(f"/api/entities/{b['id']}").json()
    assert merged_b["canonical"]["id"] == a["id"]
    # find merge id via UI-free route: reverse using the merge endpoint response
    # (recreate: the merge was made through the decision endpoint, so look it up)
    from heatseeker_common.db import session_scope
    from heatseeker_entity_resolution.models import EntityMerge
    from sqlalchemy import select

    with session_scope(engine) as session:
        merge_id = session.execute(select(EntityMerge.id)).scalar_one()
    reversed_merge = client.post(f"/api/merges/{merge_id}/reverse", json={"reason": "oops"})
    assert reversed_merge.status_code == 200
    assert len(client.get("/api/entities").json()["organisations"]) == 2
    # Reversal reopens the candidate for review.
    assert len(client.get("/api/resolution/queue").json()["candidates"]) == 1


def test_api_guards_map_to_http_statuses(engine, settings):
    client = TestClient(create_app(settings))
    a = _create(client, "Acme Scaffolding")
    b = _create(client, "Acme Holdings")

    assert client.get("/api/entities/nope").status_code == 404
    assert (
        client.post(
            "/api/entities/nope/merge", json={"absorbed_id": a["id"], "rationale": "x"}
        ).status_code
        == 404
    )
    self_merge = client.post(
        f"/api/entities/{a['id']}/merge", json={"absorbed_id": a["id"], "rationale": "x"}
    )
    assert self_merge.status_code == 409

    merged = client.post(
        f"/api/entities/{a['id']}/merge", json={"absorbed_id": b["id"], "rationale": "dupe"}
    )
    assert merged.status_code == 200
    again = client.post(
        f"/api/entities/{a['id']}/merge", json={"absorbed_id": b["id"], "rationale": "again"}
    )
    assert again.status_code == 409

    blank = client.post("/api/entities", json={"canonical_name": "   "})
    assert blank.status_code == 422


def test_ui_pages_render_and_forms_work(engine, settings):
    client = TestClient(create_app(settings))

    created = client.post(
        "/entities/create",
        data={
            "canonical_name": "Acme Scaffolding Pty Ltd",
            "organisation_type": "company",
            "identifier_scheme": "abn",
            "identifier_value": "51824753556",
            "domain": "acme.com.au",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    client.post(
        "/entities/create",
        data={"canonical_name": "Acme Scaffold Hire", "organisation_type": "company",
              "identifier_scheme": "abn", "identifier_value": "51 824 753 556"},
        follow_redirects=False,
    )

    page = client.get("/entities")
    assert page.status_code == 200
    assert "Acme Scaffolding Pty Ltd" in page.text

    scan = client.post("/entities/scan", follow_redirects=False)
    assert scan.status_code == 303

    queue_page = client.get("/resolution")
    assert queue_page.status_code == 200
    assert "exact" in queue_page.text

    org_id = client.get("/api/entities").json()["organisations"][0]["id"]
    detail = client.get(f"/entities/{org_id}")
    assert detail.status_code == 200
    assert "Merge history" in detail.text

    queue = client.get("/api/resolution/queue").json()["candidates"]
    decide = client.post(
        f"/resolution/{queue[0]['id']}/decide",
        data={"decision": "merge", "survivor_id": queue[0]["organisation_a_id"]},
        follow_redirects=False,
    )
    assert decide.status_code == 303

    absorbed_id = queue[0]["organisation_b_id"]
    merged_detail = client.get(f"/entities/{absorbed_id}")
    assert "merged into" in merged_detail.text
