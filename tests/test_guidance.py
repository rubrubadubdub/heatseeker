"""Hand-holding layer: next-steps checklist, page intros, one-click pipeline advance."""

from fastapi.testclient import TestClient
from heatseeker_api.guidance import next_steps, primary_step
from heatseeker_api.main import create_app
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job
from heatseeker_entity_resolution import entities
from heatseeker_intelligence import discovery
from heatseeker_intelligence.models import FactAssertion
from heatseeker_intelligence.observations import record_observation
from heatseeker_intelligence.pipeline import advance
from sqlalchemy import select
from test_discovery_import import CSV, MAPPING
from test_intelligence_facts import make_document, make_source


def test_next_steps_on_empty_database(engine):
    with session_scope(engine) as session:
        steps = {step.key: step for step in next_steps(session)}
        assert steps["sources"].state == "todo"
        assert steps["evidence"].state == "todo"
        assert steps["population"].state == "todo"
        assert steps["resolution"].state == "done"  # empty queue is 'done', not noise
        assert steps["conflicts"].state == "done"
        assert "failed_jobs" not in steps  # only appears when something failed
        first = primary_step(list(steps.values()))
        assert first.key == "sources"


def test_next_steps_after_import_highlight_decisions(engine, settings):
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session, settings, CSV, dataset_name="ABR", mapping=MAPPING, enqueue=False
        )
        discovery.execute_import(session, settings, run.id, CSV)

    with session_scope(engine) as session:
        steps = {step.key: step for step in next_steps(session)}
        assert steps["population"].state == "done"
        assert steps["population"].count == 3
        assert steps["evidence"].state == "done"  # the dataset file itself is evidence
        assert steps["gaps"].state == "todo"
        assert steps["gaps"].count > 0


def test_pipeline_advance_chains_stages(engine, settings):
    with session_scope(engine) as session:
        # Two similar orgs → the scan stage has work; an observation without an
        # assertion → the refresh stage has work.
        org_a = entities.create_organisation(
            session, "Acme Scaffolding Pty Ltd", identifiers=[("abn", "51824753556")]
        )
        entities.create_organisation(
            session, "Acme Scaffold Hire", identifiers=[("abn", "51824753556")]
        )
        document = make_document(session, make_source(session, "Site"))
        record_observation(
            session, document, "phone", "+61 7 3333 1111", subject_entity_id=org_a.id
        )
        org_a_id = org_a.id

    with session_scope(engine) as session:
        summary = advance(session, settings, actor="test")
        assert summary["collection_tick_queued"] is True
        assert summary["match_scan"]["candidates_created"] == 1
        assert summary["entities_refreshed"] >= 1

    with session_scope(engine) as session:
        assertion = session.execute(
            select(FactAssertion).where(
                FactAssertion.subject_entity_id == org_a_id,
                FactAssertion.predicate == "phone",
            )
        ).scalar_one()
        assert assertion.final_confidence > 0

        # Second advance: no duplicate autopilot tick, nothing left to refresh.
        summary = advance(session, settings, actor="test")
        assert summary["collection_tick_queued"] is False
        assert summary["entities_refreshed"] == 0


def test_dashboard_guidance_and_advance_button(engine, settings):
    client = TestClient(create_app(settings))
    page = client.get("/")
    assert page.status_code == 200
    assert "Next steps" in page.text
    assert "Advance pipeline" in page.text
    assert "Do this next" in page.text

    queued = client.post("/pipeline/advance", follow_redirects=False)
    assert queued.status_code == 303
    with session_scope(engine) as session:
        job = session.execute(
            select(Job).where(Job.job_type == "pipeline.advance")
        ).scalar_one()
        assert job.status == "queued"

    # A second click while one is pending doesn't stack jobs.
    again = client.post("/pipeline/advance", follow_redirects=False)
    assert again.status_code == 303
    assert "already" in again.headers["location"]
    with session_scope(engine) as session:
        count = len(
            list(
                session.execute(
                    select(Job).where(Job.job_type == "pipeline.advance")
                ).scalars()
            )
        )
        assert count == 1


def test_page_intros_orient_the_user(engine, settings):
    client = TestClient(create_app(settings))
    evidence = client.get("/evidence")
    assert "What this page is" in evidence.text
    assert "You rarely need to act here" in evidence.text
    entities_page = client.get("/entities")
    assert "What this page is" in entities_page.text
    assert "Click a company" in entities_page.text
