"""M5 over the browser UI and JSON API: discovery page, profile workspace, gap actions."""

from fastapi.testclient import TestClient
from heatseeker_api.main import create_app
from heatseeker_common.db import session_scope
from heatseeker_intelligence import discovery
from test_discovery_import import CSV, MAPPING, SOCIAL_CSV, SOCIAL_MAPPING


def _import_dataset(engine, settings):
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            CSV,
            dataset_name="ABR extract",
            mapping=MAPPING,
            publisher="ABR",
            enqueue=False,
        )
        discovery.execute_import(session, settings, run.id, CSV)


def test_profile_api_exposes_evidence_confidence_gaps(engine, settings):
    _import_dataset(engine, settings)
    client = TestClient(create_app(settings))

    organisations = client.get("/api/entities").json()["organisations"]
    acme = next(o for o in organisations if o["canonical_name"].startswith("Acme"))

    profile = client.get(f"/api/companies/{acme['id']}/profile").json()
    facts = {f["predicate"]: f for f in profile["facts"]}
    identifier_fact = facts["registration_identifier"]
    assert identifier_fact["confidence"] > 0
    assert identifier_fact["confidence_vocabulary"] in ("high", "moderate", "low")
    assert set(identifier_fact["components"]) == {
        "authority",
        "extraction",
        "match",
        "freshness",
        "corroboration",
        "contradiction",
    }
    assert identifier_fact["best_evidence_document_id"]  # evidence viewer target
    # Missing stays missing: nothing invented for phone/email.
    assert "phone" not in facts
    assert any(q["question_type"] == "missing_contact" for q in profile["research_questions"])
    sizes = {e["concept"]: e["band"] for e in profile["size_estimates"]}
    assert sizes["legal_entity_size"] == "20-49"
    assert sizes["local_branch_size"] == "unresolved"

    assert client.get("/api/companies/nope/profile").status_code == 404

    runs = client.get("/api/discovery/runs").json()["runs"]
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["imported_count"] == 3


def test_ui_discovery_page_and_import_form(engine, settings):
    client = TestClient(create_app(settings))
    page = client.get("/discovery")
    assert page.status_code == 200
    assert "bulk dataset import" in page.text.lower()
    assert "Instagram profile column" in page.text
    assert "Any source/listing record URL column" in page.text
    assert "profiles remain weak evidence" in page.text

    response = client.post(
        "/discovery/import",
        files={"dataset_file": ("abr.csv", CSV, "text/csv")},
        data={
            "dataset_name": "ABR extract",
            "publisher": "ABR",
            "col_name": "EntityName",
            "col_identifier": "ABN",
            "col_region": "State",
            "col_locality": "Town",
            "col_postcode": "Postcode",
            "col_employees_band": "Employees",
            "col_domain": "Web",
            "identifier_scheme": "abn",
            "country": "AU",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "queued" in response.headers["location"]

    with session_scope(engine) as session:
        runs = discovery.list_runs(session)
        assert len(runs) == 1
        assert runs[0].mapping["columns"]["name"] == "EntityName"
        assert runs[0].mapping["constants"]["country"] == "AU"

    listed = client.get("/discovery")
    assert "ABR extract" in listed.text


def test_public_profile_contacts_expose_direct_evidence(engine, settings):
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            SOCIAL_CSV,
            dataset_name="public profiles",
            mapping=SOCIAL_MAPPING,
            publisher="public web research",
            authority_tier=6,
            enqueue=False,
        )
        discovery.execute_import(session, settings, run.id, SOCIAL_CSV)

    client = TestClient(create_app(settings))
    organisations = client.get("/api/entities").json()["organisations"]
    acme = next(o for o in organisations if o["canonical_name"] == "Acme Scaffolding")
    profile = client.get(f"/api/companies/{acme['id']}/profile").json()
    instagram = next(
        contact
        for contact in profile["contact_points"]
        if contact["contact_type"] == "social_profile" and contact["label"] == "instagram"
    )
    assert instagram["value"] == "https://instagram.com/acme.scaffold"
    assert instagram["confidence"] == 0.45
    assert len(instagram["source_evidence_ids"]) == 1
    assert len(instagram["evidence_document_ids"]) == 1
    assert len(profile["evidence_sources"]) == 1
    assert "https://instagram.com/acme.scaffold" in profile["evidence_sources"][0]["record_urls"]

    page = client.get(f"/entities/{acme['id']}")
    assert "https://instagram.com/acme.scaffold" in page.text
    assert "Contributing sources" in page.text
    assert f"/evidence/{instagram['evidence_document_ids'][0]}" in page.text


def test_ui_entity_profile_sections_and_gap_actions(engine, settings):
    _import_dataset(engine, settings)
    client = TestClient(create_app(settings))

    organisations = client.get("/api/entities").json()["organisations"]
    acme = next(o for o in organisations if o["canonical_name"].startswith("Acme"))
    page = client.get(f"/entities/{acme['id']}")
    assert page.status_code == 200
    assert "Evidence summary" in page.text
    assert "registration_identifier" in page.text
    assert "Research gaps" in page.text
    assert "unresolved means insufficient evidence" in page.text

    profile = client.get(f"/api/companies/{acme['id']}/profile").json()
    question_id = profile["research_questions"][0]["id"]
    done = client.post(f"/research-questions/{question_id}/resolved", follow_redirects=False)
    assert done.status_code == 303
    refreshed = client.get(f"/api/companies/{acme['id']}/profile").json()
    assert question_id not in [q["id"] for q in refreshed["research_questions"]]


def test_manual_observation_requires_explicit_verification_and_links_evidence(engine, settings):
    _import_dataset(engine, settings)
    client = TestClient(create_app(settings))
    acme = next(
        organisation
        for organisation in client.get("/api/entities").json()["organisations"]
        if organisation["canonical_name"].startswith("Acme")
    )
    run = client.get("/api/discovery/runs").json()["runs"][0]

    created = client.post(
        f"/entities/{acme['id']}/observations/create",
        data={
            "source_document_id": run["source_document_id"],
            "predicate": "phone",
            "value": "+61 7 3333 1111",
            "extraction_confidence": "0.9",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    profile = client.get(f"/api/companies/{acme['id']}/profile").json()
    phone = next(fact for fact in profile["facts"] if fact["predicate"] == "phone")
    assert phone["confidence_vocabulary"] != "verified"
    assert phone["best_evidence_document_id"] == run["source_document_id"]

    page = client.get(f"/entities/{acme['id']}")
    assert "I independently verified this value" in page.text
