"""Bulk dataset import: the M5 discovery workflow (spec §9.2, §12.2)."""

import pytest
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job
from heatseeker_entity_resolution import entities
from heatseeker_intelligence import discovery
from heatseeker_intelligence.models import (
    BulkImportRun,
    FactAssertion,
    ImportRunStatus,
    Observation,
    SizeEstimate,
)
from heatseeker_source_registry.models import SourceDocument
from heatseeker_source_registry.scopes import create_scope, set_active
from sqlalchemy import select

CSV = (
    b"EntityName,ABN,State,Postcode,Town,Employees,Web\n"
    b"Acme Scaffolding Pty Ltd,51 824 753 556,QLD,4000,Brisbane,20-49,acme.com.au\n"
    b"Brisbane Formwork,98 765 432 109,QLD,4101,South Brisbane,5-19,\n"
    b",11 111 111 111,QLD,4000,Brisbane,,\n"  # rejected: no name
    b"Sydney Scaffold Co,22 222 222 222,NSW,2000,Sydney,1-4,\n"
)

MAPPING = discovery.MappingSpec(
    columns={
        "name": "EntityName",
        "identifier": "ABN",
        "region": "State",
        "postcode": "Postcode",
        "locality": "Town",
        "employees_band": "Employees",
        "domain": "Web",
    },
    constants={"identifier_scheme": "abn", "country": "AU"},
)


def _run_import(session, settings, content=CSV, mapping=MAPPING, name="ABR extract"):
    run = discovery.create_import_run(
        session,
        settings,
        content,
        dataset_name=name,
        mapping=mapping,
        filename="abr.csv",
        publisher="Australian Business Register",
        licence_note="test licence",
        enqueue=False,
    )
    return discovery.execute_import(session, settings, run.id, content)


def test_mapping_spec_validation():
    with pytest.raises(ValueError):
        discovery.MappingSpec(columns={"nope": "X"})
    with pytest.raises(ValueError):
        discovery.MappingSpec(columns={"identifier": "ABN"})  # name unbound


def test_import_creates_population_with_full_provenance(engine, settings):
    with session_scope(engine) as session:
        run = _run_import(session, settings)
        assert run.status == ImportRunStatus.SUCCEEDED
        assert run.row_count == 4
        assert run.imported_count == 3
        assert run.rejected_count == 1
        assert run.rejected_samples[0]["reason"] == "missing name"
        # §12.2 provenance: checksum, mapping, publisher, licence, stored raw file.
        assert len(run.checksum) == 64
        assert run.mapping["columns"]["name"] == "EntityName"
        document = session.get(SourceDocument, run.source_document_id)
        assert document.access_policy_snapshot["acquisition"] == "bulk_import"

    with session_scope(engine) as session:
        rows = entities.list_organisations(session)
        names = {o.canonical_name for o in rows}
        assert "Acme Scaffolding Pty Ltd" in names
        acme = next(o for o in rows if o.canonical_name.startswith("Acme"))
        assert acme.identifiers[0].value_normalised == "51824753556"
        assert acme.domains[0].domain == "acme.com.au"
        assert acme.primary_location.locality == "Brisbane"
        assert acme.provenance == "ingestion"

        # Rows became observations tied to the dataset document with row provenance.
        observations = list(
            session.execute(
                select(Observation).where(Observation.subject_entity_id == acme.id)
            ).scalars()
        )
        assert {o.predicate for o in observations} >= {
            "canonical_name", "registration_identifier", "website_domain",
            "location", "employee_count_band",
        }
        assert all(o.source_location["row"] >= 2 for o in observations)

        # Facts reconciled with confidence; sizes estimated from evidence.
        assertions = list(
            session.execute(
                select(FactAssertion).where(FactAssertion.subject_entity_id == acme.id)
            ).scalars()
        )
        assert assertions and all(a.final_confidence > 0 for a in assertions)
        sizes = {
            e.concept: e.band
            for e in session.execute(
                select(SizeEstimate).where(SizeEstimate.organisation_id == acme.id)
            ).scalars()
        }
        assert sizes["legal_entity_size"] == "20-49"

        # Dedupe funnel queued.
        job_types = {
            j.job_type for j in session.execute(select(Job)).scalars()
        }
        assert "entities.match_scan" in job_types


def test_reimport_same_file_refused_and_identifier_matching_dedupes(engine, settings):
    with session_scope(engine) as session:
        _run_import(session, settings)
    with session_scope(engine) as session, pytest.raises(ValueError, match="already imported"):
        discovery.create_import_run(
            session,
            settings,
            CSV,
            dataset_name="ABR extract again",
            mapping=MAPPING,
            enqueue=False,
        )

    # A refreshed dataset (same companies, new bytes) matches by identifier instead of
    # creating duplicates.
    refreshed = CSV + b"Acme Scaffolding,51824753556,QLD,4000,Brisbane,20-49,\n"
    with session_scope(engine) as session:
        run = _run_import(session, settings, content=refreshed, name="ABR refresh")
        assert run.matched_existing_count >= 3
        assert run.imported_count <= 1
    with session_scope(engine) as session:
        names = [o.canonical_name for o in entities.list_organisations(session)]
        assert len([n for n in names if n.lower().startswith("acme")]) == 1


def test_import_respects_active_scope(engine, settings):
    with session_scope(engine) as session:
        scope = create_scope(session, "QLD only", "AU-QLD", include_unknown=False)
        set_active(session, scope.id)

    with session_scope(engine) as session:
        run = _run_import(session, settings)
        # The NSW row is skipped and counted, not silently dropped.
        assert run.skipped_out_of_scope_count == 1
        assert run.imported_count == 2

    with session_scope(engine) as session:
        names = {o.canonical_name for o in entities.list_organisations(session)}
        assert "Sydney Scaffold Co" not in names


def test_failed_run_records_error(engine, settings):
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            CSV,
            dataset_name="ABR extract",
            mapping=MAPPING,
            enqueue=True,
        )
        run_id = run.id
        # The queued job carries the run id for the worker handler.
        job = session.execute(
            select(Job).where(Job.job_type == "discovery.import_csv")
        ).scalar_one()
        assert job.payload["run_id"] == run_id

    with session_scope(engine) as session, pytest.raises(ValueError):
        discovery.execute_import(session, settings, run_id, CSV)
        discovery.execute_import(session, settings, run_id, CSV)  # double execute


def test_execute_via_worker_handler(engine, settings):
    import logging

    from heatseeker_common.job_registry import JobContext
    from heatseeker_worker.handlers.discovery import import_csv

    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            CSV,
            dataset_name="ABR extract",
            mapping=MAPPING,
            enqueue=False,
        )
        run_id = run.id

    ctx = JobContext(
        job_id="j1",
        job_type="discovery.import_csv",
        payload={"run_id": run_id},
        attempt=1,
        engine=engine,
        logger=logging.getLogger("test"),
        settings=settings,
    )
    result = import_csv(ctx)
    assert result["imported"] == 3
    with session_scope(engine) as session:
        run = session.get(BulkImportRun, run_id)
        assert run.status == ImportRunStatus.SUCCEEDED
