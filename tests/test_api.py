from fastapi.testclient import TestClient
from heatseeker_api.main import create_app
from heatseeker_common import jobs
from heatseeker_common.db import session_scope


def test_health_endpoint_ok(engine, settings):
    client = TestClient(create_app(settings))
    for path in ("/api/health", "/health"):  # /health is the ops alias
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"]["database"]["status"] == "ok"
        assert body["checks"]["migrations"]["status"] == "ok"
        assert body["checks"]["worker"]["status"] == "absent"  # no worker running in test
        assert body["checks"]["data_paths"]["status"] == "ok"


def test_jobs_listing_and_detail(engine, settings):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo", payload={"n": 42})
        job_id = job.id

    client = TestClient(create_app(settings))

    listed = client.get("/api/jobs").json()
    assert any(item["id"] == job_id for item in listed)

    detail = client.get(f"/api/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["payload"] == {"n": 42}

    filtered = client.get("/api/jobs", params={"status": "succeeded"}).json()
    assert filtered == []


def test_job_not_found_is_404(engine, settings):
    client = TestClient(create_app(settings))
    assert client.get("/api/jobs/nonexistent").status_code == 404


def test_packs_api_lists_scaffolding(engine, settings):
    client = TestClient(create_app(settings))
    packs = client.get("/api/packs").json()
    scaffolding = next(p for p in packs if p["pack_id"] == "scaffolding_anz")
    assert scaffolding["valid"] is True
    assert scaffolding["registered_version"] is None  # not loaded in this test DB


def test_openapi_exposes_source_targeting_contracts(engine, settings):
    client = TestClient(create_app(settings))
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in (
        "/api/sources",
        "/api/sources/resolve",
        "/api/sources/{source_id}/coverages",
        "/api/source-coverages",
        "/api/source-coverages/summary",
        "/api/scopes",
        "/api/regions",
    ):
        assert path in paths


def test_regions_api_crud_and_scope_exclusions(engine, settings):
    client = TestClient(create_app(settings))

    codes = {region["code"] for region in client.get("/api/regions").json()}
    assert {"ANZ", "APAC", "LATAM", "MIDDLE_EAST", "AFRICA"} <= codes  # builtins seeded

    created = client.put(
        "/api/regions",
        json={"code": "GULF", "name": "Gulf states", "member_codes": ["AE", "SA", "QA"]},
    )
    assert created.status_code == 200
    assert created.json()["is_builtin"] is False

    scope = client.post(
        "/api/scopes",
        json={"name": "APAC ex CN", "geo_codes": ["APAC"], "exclude_codes": ["CN"]},
    )
    assert scope.status_code == 201
    assert scope.json()["exclude_codes"] == ["CN"]

    nested = client.put(
        "/api/regions", json={"code": "NESTED", "name": "n", "member_codes": ["APAC"]}
    )
    assert nested.status_code == 400  # regions may not nest

    assert client.delete("/api/regions/APAC").status_code == 409  # builtin
    assert client.delete("/api/regions/GULF").status_code == 204
    assert client.delete("/api/regions/GULF").status_code == 404  # already gone
