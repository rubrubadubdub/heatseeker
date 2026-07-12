"""Bulk dataset import: the M5 company-discovery workflow (spec §9.2, §12.2).

One CSV in, full provenance out: the raw file becomes an immutable SourceDocument, every
row value becomes an Observation, rows become (or match) Organisations through the M4
entity funnel, facts are reconciled with confidence components, and a duplicate scan is
queued so anything ambiguous lands in the resolution queue — never a silent merge.
Rows outside the active research scope are counted and skipped (scope rule for M5+).
"""

import csv
import io
from dataclasses import dataclass, field

from heatseeker_common import audit, jobs
from heatseeker_common.models import PriorityClass
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    GeographyMatchMode,
    InvalidGeographyCode,
    match_geography,
    validate_code,
)
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.models import (
    EntityProvenance,
    LocationType,
    Organisation,
    OrganisationIdentifier,
)
from heatseeker_entity_resolution.normalise import normalise_identifier, normalise_name
from heatseeker_entity_resolution.resolution import canonical_id
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
)
from heatseeker_source_registry.rawstore import read_bytes, store_bytes
from heatseeker_source_registry.scopes import active_scope
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence import facts, gaps, sizing
from heatseeker_intelligence.models import (
    BulkImportRun,
    ExtractionMethod,
    ImportRunStatus,
)
from heatseeker_intelligence.observations import (
    PREDICATE_CANONICAL_NAME,
    PREDICATE_DOMAIN,
    PREDICATE_EMPLOYEES,
    PREDICATE_IDENTIFIER,
    PREDICATE_LEGAL_NAME,
    PREDICATE_LOCATION,
    PREDICATE_REGISTRATION_STATUS,
    record_observation,
)

TRANSFORMATION_VERSION = "import/0.1"
_MAX_REJECTED_SAMPLES = 20

# Mapping fields → observation predicates handled by the importer. "columns" maps a
# field to a CSV header; "constants" fixes a field for every row (e.g. country=AU).
MAPPABLE_FIELDS = (
    "name",
    "legal_name",
    "identifier",
    "identifier_scheme",
    "registration_status",
    "domain",
    "locality",
    "region",
    "postcode",
    "country",
    "employees_band",
)


@dataclass
class RowOutcome:
    imported: bool = False
    matched_existing: bool = False
    out_of_scope: bool = False
    rejected_reason: str | None = None
    organisation_id: str | None = None


@dataclass
class MappingSpec:
    columns: dict[str, str] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        unknown = (set(self.columns) | set(self.constants)) - set(MAPPABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown mapping fields: {sorted(unknown)}")
        if "name" not in self.columns:
            raise ValueError("mapping must bind 'name' to a CSV column")

    def value(self, row: dict, field_name: str) -> str | None:
        if field_name in self.columns:
            raw = row.get(self.columns[field_name])
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        constant = self.constants.get(field_name)
        return constant.strip() if constant and constant.strip() else None

    def as_dict(self) -> dict:
        return {"columns": self.columns, "constants": self.constants}


def _bulk_source(session: Session, dataset_name: str, publisher: str | None) -> SourceDefinition:
    """One SourceDefinition per dataset, so authority/provenance stays per-publisher."""
    name = f"Bulk dataset: {dataset_name}"[:300]
    source = session.scalars(
        select(SourceDefinition).where(SourceDefinition.name == name)
    ).first()
    if source is not None:
        return source
    source = SourceDefinition(
        name=name,
        source_category="bulk_dataset",
        access_method="manual",
        authority_tier=2,  # official bulk datasets; adjust per-source in UI if needed
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.NOT_APPLICABLE,
        origin="user",
        notes=f"Created by bulk import. Publisher: {publisher or 'unknown'}",
    )
    session.add(source)
    session.flush()
    return source


def _store_dataset_document(
    session: Session,
    settings: Settings,
    source: SourceDefinition,
    content: bytes,
    *,
    filename: str,
    actor: str,
) -> tuple[SourceDocument, str]:
    rel_path, digest = store_bytes(settings, content, "text/csv")
    source_url = f"import://{source.id}/{digest}"
    existing = session.scalars(
        select(SourceDocument).where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.content_hash == digest,
        )
    ).first()
    if existing is not None:
        existing.last_seen_at = utc_now()
        existing.retrieval_count += 1
        return existing, digest
    document = SourceDocument(
        source_definition_id=source.id,
        source_url=source_url,
        content_hash=digest,
        content_type="text/csv",
        original_filename=filename[:500] or "dataset.csv",
        size_bytes=len(content),
        raw_storage_path=rel_path,
        access_policy_snapshot={
            "acquisition": "bulk_import",
            "actor": actor,
            "robots_status": "not_applicable",
            "robots_enforced": False,
            "terms_status": str(source.terms_status),
        },
        targeting_snapshot={
            "schema_version": 1,
            "mode": "bulk_import",
            "coverage_ids": [],
            "coverages": [],
            "research_scopes": [],
        },
        collector_version=TRANSFORMATION_VERSION,
    )
    session.add(document)
    session.flush()
    return document, digest


def _row_geo_codes(mapping: MappingSpec, row: dict) -> list[str]:
    """Most specific single code for the row — a bare country would hierarchically
    overlap every state and defeat sub-national scope filters."""
    country = (mapping.value(row, "country") or "").upper()
    region = (mapping.value(row, "region") or "").upper()
    candidates = []
    if country and region:
        candidates.append(f"{country}-{region}")
    if country:
        candidates.append(country)
    for raw in candidates:
        try:
            return [validate_code(raw)]
        except InvalidGeographyCode:
            continue
    return []


def _in_scope(session: Session, mapping: MappingSpec, row: dict) -> bool:
    scope = active_scope(session)
    if scope is None or not (scope.geo_codes or scope.exclude_codes):
        return True
    codes = _row_geo_codes(mapping, row)
    exclude = list(scope.exclude_codes or [])
    if codes and exclude and match_geography(codes, exclude, mode=GeographyMatchMode.OVERLAPS):
        return False
    return match_geography(
        codes,
        list(scope.geo_codes or []),
        mode=GeographyMatchMode.OVERLAPS,
        include_unknown=bool(scope.include_unknown),
    )


def _find_organisation(
    session: Session,
    *,
    scheme: str | None,
    identifier: str | None,
    name: str,
    locality: str | None,
) -> Organisation | None:
    if scheme and identifier:
        match = session.execute(
            select(OrganisationIdentifier).where(
                OrganisationIdentifier.scheme == scheme,
                OrganisationIdentifier.value_normalised == normalise_identifier(identifier),
            )
        ).scalars().first()
        if match is not None:
            return session.get(Organisation, canonical_id(session, match.organisation_id))
    name_key = normalise_name(name)
    candidates = session.execute(
        select(Organisation).where(Organisation.merged_into_id.is_(None))
    ).scalars()
    for organisation in candidates:
        if normalise_name(organisation.canonical_name) != name_key:
            continue
        if locality and organisation.primary_location is not None:
            org_locality = (organisation.primary_location.locality or "").casefold()
            if org_locality and org_locality != locality.casefold():
                continue  # same name, different place — let the match scan decide
        return organisation
    return None


def _import_row(
    session: Session,
    document: SourceDocument,
    mapping: MappingSpec,
    row: dict,
    row_number: int,
) -> RowOutcome:
    name = mapping.value(row, "name")
    if not name:
        return RowOutcome(rejected_reason="missing name")
    if not _in_scope(session, mapping, row):
        return RowOutcome(out_of_scope=True)

    scheme = (mapping.value(row, "identifier_scheme") or "").lower() or None
    identifier = mapping.value(row, "identifier")
    locality = mapping.value(row, "locality")
    country = (mapping.value(row, "country") or "").upper() or None

    organisation = _find_organisation(
        session, scheme=scheme, identifier=identifier, name=name, locality=locality
    )
    matched = organisation is not None
    if organisation is None:
        organisation = entities.create_organisation(
            session,
            name,
            legal_name=mapping.value(row, "legal_name"),
            country_of_registration=country,
            provenance=EntityProvenance.INGESTION,
        )

    location_kwargs = {"row": row_number}

    def observe(predicate: str, value, confidence: float = 0.9):
        return record_observation(
            session,
            document,
            predicate,
            value,
            subject_entity_id=organisation.id,
            extraction_method=ExtractionMethod.IMPORT,
            extraction_confidence=confidence,
            source_location=location_kwargs,
        )

    observe(PREDICATE_CANONICAL_NAME, name)
    legal_name = mapping.value(row, "legal_name")
    if legal_name:
        observe(PREDICATE_LEGAL_NAME, legal_name)
    if scheme and identifier:
        observe(PREDICATE_IDENTIFIER, {"scheme": scheme, "value": identifier})
        entities.add_identifier(session, organisation, scheme, identifier, country=country)
    status_value = mapping.value(row, "registration_status")
    if status_value:
        observe(PREDICATE_REGISTRATION_STATUS, status_value.lower())
    domain = mapping.value(row, "domain")
    if domain:
        try:
            entities.add_domain(session, organisation, domain)
            observe(PREDICATE_DOMAIN, domain.lower())
        except ValueError:
            pass  # unusable domain cell — name row still imports
    if locality or mapping.value(row, "postcode") or country:
        observe(
            PREDICATE_LOCATION,
            {
                "locality": locality,
                "region": mapping.value(row, "region"),
                "postcode": mapping.value(row, "postcode"),
                "country": country,
            },
            confidence=0.85,
        )
        if organisation.primary_location_id is None and locality:
            location = entities.add_location(
                session,
                locality=locality,
                region=mapping.value(row, "region"),
                postal_code=mapping.value(row, "postcode"),
                country=country,
                location_type=LocationType.REGISTERED_ADDRESS,
            )
            entities.set_primary_location(session, organisation, location)
    employees = mapping.value(row, "employees_band")
    if employees and employees in sizing.EMPLOYEE_BANDS:
        observe(PREDICATE_EMPLOYEES, employees, confidence=0.85)

    return RowOutcome(
        imported=not matched, matched_existing=matched, organisation_id=organisation.id
    )


def execute_import(
    session: Session, settings: Settings, run_id: str, content: bytes
) -> BulkImportRun:
    """Run one queued import. Content is re-supplied from the stored raw document."""
    run = session.get(BulkImportRun, run_id)
    if run is None:
        raise LookupError(f"import run not found: {run_id}")
    if run.status not in (ImportRunStatus.QUEUED, ImportRunStatus.RUNNING):
        raise ValueError(f"import run already {run.status}")
    run.status = ImportRunStatus.RUNNING
    session.flush()

    mapping = MappingSpec(**run.mapping)
    document = session.get(SourceDocument, run.source_document_id)
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    touched: set[str] = set()
    rejected_samples: list[dict] = []
    counts = {"rows": 0, "imported": 0, "matched": 0, "out_of_scope": 0, "rejected": 0}
    for row_number, row in enumerate(reader, start=2):  # header is line 1
        counts["rows"] += 1
        outcome = _import_row(session, document, mapping, row, row_number)
        if outcome.rejected_reason:
            counts["rejected"] += 1
            if len(rejected_samples) < _MAX_REJECTED_SAMPLES:
                rejected_samples.append(
                    {"row": row_number, "reason": outcome.rejected_reason}
                )
        elif outcome.out_of_scope:
            counts["out_of_scope"] += 1
        else:
            counts["imported" if outcome.imported else "matched"] += 1
            if outcome.organisation_id:
                touched.add(outcome.organisation_id)

    for organisation_id in sorted(touched):
        facts.reconcile_all(session, organisation_id)
        sizing.estimate_sizes(session, organisation_id)
        gaps.generate_for(session, organisation_id)

    run.row_count = counts["rows"]
    run.imported_count = counts["imported"]
    run.matched_existing_count = counts["matched"]
    run.skipped_out_of_scope_count = counts["out_of_scope"]
    run.rejected_count = counts["rejected"]
    run.rejected_samples = rejected_samples
    run.transformation_version = TRANSFORMATION_VERSION
    run.status = ImportRunStatus.SUCCEEDED
    run.finished_at = utc_now()

    if touched:
        jobs.enqueue(
            session,
            "entities.match_scan",
            priority=PriorityClass.BACKGROUND_ENRICHMENT,
            actor=run.actor,
        )
    audit.record(
        session,
        run.actor,
        "discovery.import_completed",
        "bulk_import_run",
        run.id,
        {**counts, "dataset": run.dataset_name},
    )
    session.flush()
    return run


def create_import_run(
    session: Session,
    settings: Settings,
    content: bytes,
    *,
    dataset_name: str,
    mapping: MappingSpec,
    filename: str = "dataset.csv",
    publisher: str | None = None,
    dataset_version: str | None = None,
    coverage_date: str | None = None,
    licence_note: str | None = None,
    actor: str = "user",
    enqueue: bool = True,
) -> BulkImportRun:
    """Store the file with provenance and queue the import job (§12.2)."""
    if not content:
        raise ValueError("dataset file is empty")
    if not dataset_name.strip():
        raise ValueError("dataset name is required")
    source = _bulk_source(session, dataset_name.strip(), publisher)
    document, digest = _store_dataset_document(
        session, settings, source, content, filename=filename, actor=actor
    )
    duplicate = session.scalars(
        select(BulkImportRun).where(
            BulkImportRun.checksum == digest,
            BulkImportRun.status == ImportRunStatus.SUCCEEDED,
        )
    ).first()
    if duplicate is not None:
        raise ValueError(
            f"this exact file was already imported on {duplicate.created_at.date()} "
            f"(run {duplicate.id}) — re-importing would duplicate observations"
        )
    run = BulkImportRun(
        dataset_name=dataset_name.strip(),
        publisher=(publisher or "").strip() or None,
        dataset_version=(dataset_version or "").strip() or None,
        coverage_date=(coverage_date or "").strip() or None,
        licence_note=(licence_note or "").strip() or None,
        checksum=digest,
        mapping=mapping.as_dict(),
        source_definition_id=source.id,
        source_document_id=document.id,
        actor=actor,
    )
    session.add(run)
    session.flush()
    if enqueue:
        jobs.enqueue(
            session,
            "discovery.import_csv",
            payload={"run_id": run.id},
            priority=PriorityClass.INTERACTIVE,
            actor=actor,
        )
    audit.record(
        session,
        actor,
        "discovery.import_queued",
        "bulk_import_run",
        run.id,
        {"dataset": run.dataset_name, "checksum": digest[:12]},
    )
    return run


def load_run_content(settings: Settings, session: Session, run: BulkImportRun) -> bytes:
    document = session.get(SourceDocument, run.source_document_id)
    if document is None:
        raise LookupError("import run has no stored dataset document")
    return read_bytes(settings, document.raw_storage_path)


def list_runs(session: Session, limit: int = 50) -> list[BulkImportRun]:
    return list(
        session.execute(
            select(BulkImportRun).order_by(BulkImportRun.created_at.desc()).limit(limit)
        ).scalars()
    )
