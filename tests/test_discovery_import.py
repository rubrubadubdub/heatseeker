"""Bulk dataset import: the M5 discovery workflow (spec §9.2, §12.2)."""

import io
import zipfile

import pytest
from heatseeker_common.db import session_scope
from heatseeker_common.models import Job
from heatseeker_entity_resolution import entities
from heatseeker_intelligence import discovery
from heatseeker_intelligence.models import (
    BulkImportRun,
    CapabilityAssignment,
    ClassificationAssignment,
    FactAssertion,
    ImportRunStatus,
    Observation,
    SizeEstimate,
)
from heatseeker_source_registry.models import SourceDefinition, SourceDocument
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
    with pytest.raises(ValueError, match="require a pack_id"):
        discovery.MappingSpec(columns={"name": "Name", "service_claim": "Services"})

    geography_mapping = discovery.MappingSpec(
        columns={"name": "Name", "region": "State"}, constants={"country": "AU"}
    )
    assert discovery._row_geo_codes(
        geography_mapping, {"Name": "A", "State": "New South Wales"}
    ) == ["AU-NSW"]
    assert discovery._row_geo_codes(
        geography_mapping, {"Name": "B", "State": "not-a-region"}
    ) == []


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
        assert run.authority_tier == 5  # conservative unless the operator declares otherwise

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


def test_single_csv_zip_import_preserves_archive_and_rejects_ambiguity(engine, settings):
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/companies.csv", CSV)
    content = archive_bytes.getvalue()
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            content,
            dataset_name="Zipped extract",
            mapping=MAPPING,
            filename="companies.zip",
            enqueue=False,
        )
        discovery.execute_import(session, settings, run.id, content)
        document = session.get(SourceDocument, run.source_document_id)
        assert run.imported_count == 3
        assert document.content_type == "application/zip"
        assert document.original_filename == "companies.zip"

    ambiguous = io.BytesIO()
    with zipfile.ZipFile(ambiguous, "w") as archive:
        archive.writestr("one.csv", b"Name\nOne\n")
        archive.writestr("two.csv", b"Name\nTwo\n")
    with pytest.raises(ValueError, match="exactly one CSV"):
        discovery._csv_payload(settings, ambiguous.getvalue())


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


def test_import_uses_queued_scope_snapshot_not_later_active_scope(engine, settings):
    with session_scope(engine) as session:
        qld = create_scope(session, "QLD only", "AU-QLD", include_unknown=False)
        nsw = create_scope(session, "NSW only", "AU-NSW", include_unknown=False)
        set_active(session, qld.id)
        run = discovery.create_import_run(
            session,
            settings,
            CSV,
            dataset_name="Snapshot scope",
            mapping=MAPPING,
            enqueue=False,
        )
        assert run.scope_snapshot["id"] == qld.id
        set_active(session, nsw.id)
        discovery.execute_import(session, settings, run.id, CSV)
        assert run.imported_count == 2
        assert run.skipped_out_of_scope_count == 1


def test_same_name_without_complete_geography_never_auto_matches(engine, settings):
    with session_scope(engine) as session:
        entities.create_organisation(session, "Smith Scaffolding")
        content = b"Name,State,Town\nSmith Scaffolding,NSW,Sydney\n"
        mapping = discovery.MappingSpec(
            columns={"name": "Name", "region": "State", "locality": "Town"},
            constants={"country": "AU"},
        )
        run = discovery.create_import_run(
            session,
            settings,
            content,
            dataset_name="Name collision",
            mapping=mapping,
            enqueue=False,
        )
        discovery.execute_import(session, settings, run.id, content)
        assert run.imported_count == 1
        assert run.matched_existing_count == 0
        assert len(entities.list_organisations(session)) == 2


def test_declared_authority_is_explicit_and_publisher_scoped(engine, settings):
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            b"Name\nOfficial Co\n",
            dataset_name="Registry extract",
            publisher="Government Registry",
            mapping=discovery.MappingSpec(columns={"name": "Name"}),
            authority_tier=2,
            enqueue=False,
        )
        source = session.get(SourceDefinition, run.source_definition_id)
        assert run.authority_tier == 2
        assert source.authority_tier == 2
        assert "Government Registry" in source.name
        with pytest.raises(ValueError, match="authority tier 2"):
            discovery.create_import_run(
                session,
                settings,
                b"Name\nSecond Co\n",
                dataset_name="Registry extract",
                publisher="Government Registry",
                mapping=discovery.MappingSpec(columns={"name": "Name"}),
                authority_tier=5,
                enqueue=False,
            )


def test_import_populates_pack_classifications_and_capabilities(engine, settings):
    content = (
        b"Name,ABN,State,Town,Services,Archetype\n"
        b"Acme Access,123,QLD,Brisbane,scaffold_design|hire,scaffold_contractor\n"
    )
    mapping = discovery.MappingSpec(
        columns={
            "name": "Name",
            "identifier": "ABN",
            "region": "State",
            "locality": "Town",
            "service_claim": "Services",
            "archetype_claim": "Archetype",
        },
        constants={
            "identifier_scheme": "abn",
            "country": "AU",
            "pack_id": "scaffolding_anz",
        },
    )
    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            content,
            dataset_name="Industry member extract",
            mapping=mapping,
            enqueue=False,
        )
        discovery.execute_import(session, settings, run.id, content)
        assert run.pack_snapshot["id"] == "scaffolding_anz"
        assert run.pack_snapshot["content_hash"]
        assignments = list(session.execute(select(ClassificationAssignment)).scalars())
        capabilities = list(session.execute(select(CapabilityAssignment)).scalars())
        assert {assignment.category_id for assignment in assignments} == {
            "scaffold_design",
            "hire",
            "scaffold_contractor",
        }
        assert {capability.capability_id for capability in capabilities} == {
            "scaffold_design",
            "hire",
        }
        from heatseeker_intelligence import profile

        organisation_id = assignments[0].entity_id
        assembled = profile.assemble(session, organisation_id)
        assert all(
            assembled["classification_evidence"].get(assignment.id)
            for assignment in assignments
        )
        assert all(
            assembled["capability_evidence"].get(capability.id)
            for capability in capabilities
        )
        assert not list(
            session.execute(
                select(FactAssertion).where(
                    FactAssertion.predicate.in_(["service_claim", "archetype_claim"])
                )
            ).scalars()
        )


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


def test_worker_failure_status_survives_reraise(engine, settings):
    import logging

    from heatseeker_common.job_registry import JobContext
    from heatseeker_worker.handlers.discovery import import_csv

    with session_scope(engine) as session:
        run = discovery.create_import_run(
            session,
            settings,
            b"Name\nBroken Co\n",
            dataset_name="Broken import",
            mapping=discovery.MappingSpec(columns={"name": "Name"}),
            enqueue=False,
        )
        run.source_document_id = None
        run_id = run.id

    ctx = JobContext(
        job_id="failed-import",
        job_type="discovery.import_csv",
        payload={"run_id": run_id},
        attempt=1,
        engine=engine,
        logger=logging.getLogger("test"),
        settings=settings,
    )
    with pytest.raises(LookupError):
        import_csv(ctx)
    with session_scope(engine) as session:
        failed = session.get(BulkImportRun, run_id)
        assert failed.status == ImportRunStatus.FAILED
        assert "stored dataset document" in failed.error
        assert failed.finished_at is not None
