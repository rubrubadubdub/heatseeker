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
    ):
        assert path in paths
