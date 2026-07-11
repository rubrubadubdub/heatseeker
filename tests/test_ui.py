"""Browser GUI routes (ADR-0009): every page renders; actions round-trip."""

import pytest
from fastapi.testclient import TestClient
from heatseeker_api.main import create_app
from heatseeker_common import jobs
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job, JobStatus


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings))


def test_all_pages_render(client):
    for path in (
        "/",
        "/jobs",
        "/packs",
        "/backups",
        "/health-ui",
        "/sources",
        "/evidence",
        "/scopes",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Heatseeker" in response.text


def test_sources_workflow_via_ui(client):
    # Sync seeds from packs -> sources appear as candidates with scope badges.
    response = client.post("/sources/sync-seeds", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/sources").text
    assert "Australian Business Register" in page
    assert "candidate" in page
    # Default ANZ scope marks the US-less seed list mostly in scope; global rows too.
    assert "Research scope" in page


def test_scope_create_and_activate_via_ui(client):
    response = client.post(
        "/scopes/create",
        data={"name": "QLD only", "codes": "AU-QLD", "description": "test"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/scopes").text
    assert "QLD only" in page
    assert "AU-QLD" in page


def test_scope_create_with_exclusions_via_ui(client):
    response = client.post(
        "/scopes/create",
        data={"name": "APAC ex China", "codes": "APAC", "exclude": "CN"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/scopes").text
    assert "APAC ex China" in page
    assert "not CN" in page  # exclusion badge


def test_region_editor_round_trip(client):
    page = client.get("/scopes").text
    assert "Named regions" in page
    assert "LATAM" in page  # builtins seeded and listed

    response = client.post(
        "/scopes/regions/save",
        data={"code": "GULF", "name": "Gulf states", "members": "AE, SA, QA"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "GULF" in client.get("/scopes").text

    # A custom region is immediately usable in a scope…
    client.post(
        "/scopes/create", data={"name": "Gulf scope", "codes": "GULF"}, follow_redirects=False
    )
    # …and deletion is refused while referenced, as is deleting builtins.
    client.post("/scopes/regions/GULF/delete", follow_redirects=False)
    client.post("/scopes/regions/APAC/delete", follow_redirects=False)
    page = client.get("/scopes").text
    assert "GULF" in page
    assert "APAC" in page


def test_dashboard_shows_pack_and_health(client):
    html = client.get("/").text
    assert "scaffolding_anz" in html
    assert "System health" in html


def test_static_assets_served(client):
    for asset in ("bootstrap.min.css", "bootstrap.bundle.min.js", "htmx.min.js"):
        assert client.get(f"/static/vendor/{asset}").status_code == 200


def test_enqueue_job_via_ui(client, engine):
    response = client.post(
        "/jobs/enqueue",
        data={"job_type": "demo.echo", "payload": '{"from": "ui"}', "priority": "50"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope(engine) as session:
        job = session.query(Job).one()
        assert job.job_type == "demo.echo"
        assert job.payload == {"from": "ui"}


def test_enqueue_rejects_bad_payload(client, engine):
    response = client.post(
        "/jobs/enqueue",
        data={"job_type": "demo.echo", "payload": "not json"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Invalid%20payload" in response.headers["location"]
    with session_scope(engine) as session:
        assert session.query(Job).count() == 0


def test_cancel_job_via_ui(client, engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.sleep")
        job_id = job.id
    response = client.post(f"/jobs/{job_id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    with session_scope(engine) as session:
        assert session.get(Job, job_id).status == JobStatus.CANCELLED


def test_job_detail_and_table_partial(client, engine):
    with session_scope(engine) as session:
        job = jobs.enqueue(session, "demo.echo", payload={"detail": True})
        job_id = job.id
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "demo.echo" in detail.text

    partial = client.get("/jobs/table")
    assert partial.status_code == 200
    assert job_id[:8] in partial.text
    assert "<html" not in partial.text  # partial, not a full page

    missing = client.get("/jobs/no-such-job")
    assert missing.status_code == 200
    assert "Job not found" in missing.text


def test_pack_pages_and_load_action(client, engine):
    packs_page = client.get("/packs")
    assert "scaffolding_anz" in packs_page.text
    assert "not loaded" in packs_page.text

    response = client.post("/packs/scaffolding_anz/load", follow_redirects=False)
    assert response.status_code == 303

    detail = client.get("/packs/scaffolding_anz")
    assert detail.status_code == 200
    assert "Scaffolding, Access &amp; Temporary Works" in detail.text
    assert "registered" in detail.text

    assert client.post("/packs/nonexistent/load", follow_redirects=False).status_code == 303


def test_backup_create_via_ui(client, settings):
    response = client.post("/backups/create", follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/backups")
    assert "heatseeker" in page.text.lower()
    assert len(list(settings.backups_dir.iterdir())) == 1
