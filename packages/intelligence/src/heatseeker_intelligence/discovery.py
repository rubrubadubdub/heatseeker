"""Bulk dataset import: the M5 company-discovery workflow (spec §9.2, §12.2).

One CSV in, full provenance out: the raw file becomes an immutable SourceDocument, every
row value becomes an Observation, rows become (or match) Organisations through the M4
entity funnel, facts are reconciled with confidence components, and a duplicate scan is
queued so anything ambiguous lands in the resolution queue — never a silent merge.
Rows outside the active research scope are counted and skipped (scope rule for M5+).
"""

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from heatseeker_common import audit, jobs
from heatseeker_common.models import PriorityClass
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import (
    KNOWN_CODES,
    GeographyMatchMode,
    InvalidGeographyCode,
    match_geography,
    validate_code,
)
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.models import (
    ContactType,
    EntityProvenance,
    LocationType,
    Organisation,
    OrganisationIdentifier,
)
from heatseeker_entity_resolution.normalise import (
    normalise_identifier,
    normalise_name,
    phone_match_key,
)
from heatseeker_entity_resolution.resolution import canonical_id
from heatseeker_industry_packs.loader import default_packs_root, discover_packs, load_pack
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
)
from heatseeker_source_registry.rawstore import read_bytes, store_bytes
from heatseeker_source_registry.scopes import active_scope
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from heatseeker_intelligence import capabilities, classifications, facts, gaps, sizing
from heatseeker_intelligence.models import (
    BulkImportRun,
    ExtractionMethod,
    ImportRunStatus,
    NormalisationStatus,
)
from heatseeker_intelligence.observations import (
    PREDICATE_ARCHETYPE_CLAIM,
    PREDICATE_CANONICAL_NAME,
    PREDICATE_DOMAIN,
    PREDICATE_EMAIL,
    PREDICATE_EMPLOYEES,
    PREDICATE_IDENTIFIER,
    PREDICATE_LEGAL_NAME,
    PREDICATE_LOCATION,
    PREDICATE_PHONE,
    PREDICATE_REGISTRATION_STATUS,
    PREDICATE_SERVICE_CLAIM,
    record_observation,
)

TRANSFORMATION_VERSION = "import/0.3"
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
    "email",
    "phone",
    "street_address",
    "service_claim",
    "archetype_claim",
    "pack_id",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Local parts that mark a role-based route (§20.2 prefers these over general boxes).
_ROLE_LOCAL_PARTS = {
    "estimating",
    "tenders",
    "design",
    "drafting",
    "engineering",
    "projects",
    "operations",
    "commercial",
    "procurement",
}

_REGION_ALIASES = {
    ("AU", "AUSTRALIAN CAPITAL TERRITORY"): "ACT",
    ("AU", "NEW SOUTH WALES"): "NSW",
    ("AU", "NORTHERN TERRITORY"): "NT",
    ("AU", "QUEENSLAND"): "QLD",
    ("AU", "SOUTH AUSTRALIA"): "SA",
    ("AU", "TASMANIA"): "TAS",
    ("AU", "VICTORIA"): "VIC",
    ("AU", "WESTERN AUSTRALIA"): "WA",
}


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
        claim_fields = {"service_claim", "archetype_claim"} & set(self.columns)
        if claim_fields and not self.constants.get("pack_id", "").strip():
            raise ValueError("service/archetype claim columns require a pack_id")

    def value(self, row: dict, field_name: str) -> str | None:
        if field_name in self.columns:
            raw = row.get(self.columns[field_name])
            if raw is not None and str(raw).strip():
                return str(raw).strip()
        constant = self.constants.get(field_name)
        return constant.strip() if constant and constant.strip() else None

    def as_dict(self) -> dict:
        return {"columns": self.columns, "constants": self.constants}


@dataclass
class _OrganisationIndex:
    """Import-local identity index; avoids scanning every organisation for every row."""

    identifiers: dict[tuple[str, str], Organisation] = field(default_factory=dict)
    names_and_places: dict[tuple[str, str, str], Organisation] = field(default_factory=dict)
    ambiguous_names_and_places: set[tuple[str, str, str]] = field(default_factory=set)

    @classmethod
    def build(cls, session: Session) -> "_OrganisationIndex":
        index = cls()
        organisations = session.scalars(
            select(Organisation)
            .where(Organisation.merged_into_id.is_(None))
            .options(selectinload(Organisation.identifiers))
        ).all()
        for organisation in organisations:
            index.add(organisation)
        return index

    def add(self, organisation: Organisation) -> None:
        for identifier in organisation.identifiers:
            self.identifiers[(identifier.scheme, identifier.value_normalised)] = organisation
        location = organisation.primary_location
        if location is None or not location.locality or not location.country:
            return
        key = (
            normalise_name(organisation.canonical_name),
            location.locality.casefold(),
            location.country.casefold(),
        )
        if key in self.ambiguous_names_and_places:
            return
        existing = self.names_and_places.get(key)
        if existing is not None and existing.id != organisation.id:
            self.names_and_places.pop(key, None)
            self.ambiguous_names_and_places.add(key)
        else:
            self.names_and_places[key] = organisation


def _parse_coverage_date(value: str | None) -> datetime | None:
    """Parse dataset currency separately from retrieval/import time."""

    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("coverage_date must be ISO 8601 (for example 2026-07-18)") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _bulk_source(
    session: Session,
    dataset_name: str,
    publisher: str | None,
    authority_tier: int,
) -> SourceDefinition:
    """One SourceDefinition per dataset, so authority/provenance stays per-publisher."""
    publisher_name = (publisher or "").strip()
    source_identity = f"{publisher_name}: {dataset_name}" if publisher_name else dataset_name
    name = f"Bulk dataset: {source_identity}"[:300]
    source = session.scalars(
        select(SourceDefinition).where(SourceDefinition.name == name)
    ).first()
    if source is not None:
        if source.authority_tier != authority_tier:
            raise ValueError(
                f"existing source '{name}' is authority tier {source.authority_tier}; "
                "choose that tier or edit the source registry first"
            )
        return source
    source = SourceDefinition(
        name=name,
        source_category="bulk_dataset",
        access_method="manual",
        authority_tier=authority_tier,
        lifecycle_status=SourceLifecycle.ACTIVE,
        robots_status=RobotsStatus.NOT_APPLICABLE,
        origin="user",
        notes=(
            f"Created by bulk import. Publisher: {publisher_name or 'unknown'}. "
            f"Authority tier explicitly declared as {authority_tier}."
        ),
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
    scope_snapshot: dict | None,
    pack_snapshot: dict | None,
    authority_tier: int,
) -> tuple[SourceDocument, str]:
    content_type = "application/zip" if zipfile.is_zipfile(io.BytesIO(content)) else "text/csv"
    rel_path, digest = store_bytes(settings, content, content_type)
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
        content_type=content_type,
        original_filename=filename[:500] or "dataset.csv",
        size_bytes=len(content),
        raw_storage_path=rel_path,
        access_policy_snapshot={
            "acquisition": "bulk_import",
            "actor": actor,
            "robots_status": "not_applicable",
            "robots_enforced": False,
            "terms_status": str(source.terms_status),
            "authority_tier": authority_tier,
        },
        targeting_snapshot={
            "schema_version": 1,
            "mode": "bulk_import",
            "coverage_ids": [],
            "coverages": [],
            "research_scopes": [scope_snapshot] if scope_snapshot else [],
            "industry_pack": pack_snapshot,
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
    region = _REGION_ALIASES.get((country, region), region)
    candidates = []
    if country and region:
        candidates.append(f"{country}-{region}")
    if country:
        candidates.append(country)
    for index, raw in enumerate(candidates):
        try:
            code = validate_code(raw)
            if index == 0 and country in {"AU", "NZ"} and code not in KNOWN_CODES:
                return []
            return [code]
        except InvalidGeographyCode:
            if index == 0 and region:
                return []  # unknown region, never silently broaden to the whole country
            continue
    return []


def _scope_snapshot(session: Session) -> dict | None:
    scope = active_scope(session)
    if scope is None:
        return None
    return {
        "id": scope.id,
        "name": scope.name,
        "geo_codes": list(scope.geo_codes or []),
        "exclude_codes": list(scope.exclude_codes or []),
        "include_unknown": bool(scope.include_unknown),
        "industry_ids": list(scope.industry_ids or []),
        "target_filters": dict(scope.target_filters or {}),
    }


def _in_scope(
    scope: dict | None, mapping: MappingSpec, row: dict, *, pack_id: str | None
) -> bool:
    if scope is None:
        return True
    include_unknown = bool(scope.get("include_unknown"))

    requested_geography = list(scope.get("geo_codes") or [])
    excluded_geography = list(scope.get("exclude_codes") or [])
    if requested_geography or excluded_geography:
        codes = _row_geo_codes(mapping, row)
        if codes and excluded_geography and match_geography(
            codes, excluded_geography, mode=GeographyMatchMode.OVERLAPS
        ):
            return False
        if requested_geography and not match_geography(
            codes,
            requested_geography,
            mode=GeographyMatchMode.OVERLAPS,
            include_unknown=include_unknown,
        ):
            return False

    requested_industries = {
        str(value).strip().casefold() for value in scope.get("industry_ids") or [] if value
    }
    row_pack = (mapping.value(row, "pack_id") or pack_id or "").strip().casefold()
    if requested_industries and (
        (not row_pack and not include_unknown)
        or (row_pack and row_pack not in requested_industries)
    ):
        return False

    row_facets = {
        "industry": [row_pack] if row_pack else [],
        "service": _split_claims(mapping.value(row, "service_claim")),
        "capability": _split_claims(mapping.value(row, "service_claim")),
        "archetype": _split_claims(mapping.value(row, "archetype_claim")),
        "company_archetype": _split_claims(mapping.value(row, "archetype_claim")),
    }
    for raw_dimension, raw_wanted in (scope.get("target_filters") or {}).items():
        dimension = str(raw_dimension).strip().casefold()
        if isinstance(raw_wanted, dict):
            include_values = raw_wanted.get("include") or []
            exclude_values = raw_wanted.get("exclude") or []
        else:
            include_values = raw_wanted if isinstance(raw_wanted, (list, tuple)) else [raw_wanted]
            exclude_values = []
        wanted = {str(value).strip().casefold() for value in include_values if value}
        excluded = {str(value).strip().casefold() for value in exclude_values if value}
        if not wanted and not excluded:
            continue
        known = row_facets.get(dimension)
        if known is None:
            if not include_unknown:
                return False
            continue
        present = {value.casefold() for value in known}
        if present.intersection(excluded):
            return False
        if present:
            if wanted and not present.intersection(wanted):
                return False
        elif wanted and not include_unknown:
            return False
    return True


def _find_organisation(
    session: Session,
    *,
    scheme: str | None,
    identifier: str | None,
    name: str,
    locality: str | None,
    country: str | None,
    index: _OrganisationIndex | None = None,
) -> Organisation | None:
    if scheme and identifier:
        key = (scheme, normalise_identifier(identifier))
        if index is not None and key in index.identifiers:
            return index.identifiers[key]
        match = session.execute(
            select(OrganisationIdentifier).where(
                OrganisationIdentifier.scheme == scheme,
                OrganisationIdentifier.value_normalised == normalise_identifier(identifier),
            )
        ).scalars().first()
        if match is not None:
            return session.get(Organisation, canonical_id(session, match.organisation_id))
    # A name is not a deterministic identity. Require matching non-empty geography;
    # ambiguous names become separate records for the M4 review queue.
    if not locality or not country:
        return None
    name_key = normalise_name(name)
    if index is not None:
        return index.names_and_places.get((name_key, locality.casefold(), country.casefold()))
    candidates = session.execute(
        select(Organisation).where(Organisation.merged_into_id.is_(None))
    ).scalars()
    for organisation in candidates:
        if normalise_name(organisation.canonical_name) != name_key:
            continue
        if organisation.primary_location is None:
            continue
        org_locality = (organisation.primary_location.locality or "").casefold()
        org_country = (organisation.primary_location.country or "").casefold()
        if not org_locality or not org_country:
            continue
        if org_locality != locality.casefold() or org_country != country.casefold():
            continue
        return organisation
    return None


def _split_claims(raw: str | None) -> list[str]:
    if not raw:
        return []
    return sorted({part.strip() for part in re.split(r"[|;,]", raw) if part.strip()})


def _pack_vocabulary(
    pack_id: str | None, snapshot: dict | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    if not pack_id:
        return {}, {}
    if snapshot and snapshot.get("id") == pack_id:
        return dict(snapshot.get("services") or {}), dict(snapshot.get("archetypes") or {})
    available = {path.name for path in discover_packs()}
    if pack_id not in available:
        raise ValueError(f"unknown industry pack: {pack_id}")
    pack = load_pack(default_packs_root() / pack_id)
    service_file = pack.files.get("service_taxonomy.yaml")
    archetype_file = pack.files.get("company_archetypes.yaml")
    services = {
        service.id: service.name
        for category in (service_file.categories if service_file else [])
        for service in category.services
    }
    archetypes = {
        archetype.id: archetype.name
        for archetype in (archetype_file.archetypes if archetype_file else [])
    }
    return services, archetypes


def _pack_snapshot(pack_id: str | None) -> dict | None:
    if not pack_id:
        return None
    services, archetypes = _pack_vocabulary(pack_id)
    pack = load_pack(default_packs_root() / pack_id)
    return {
        "id": pack.pack_id,
        "version": pack.version,
        "content_hash": pack.content_hash,
        "services": services,
        "archetypes": archetypes,
    }


def _csv_payload(settings: Settings, content: bytes) -> bytes:
    """Plain CSV or one bounded CSV member from a ZIP archive."""
    if not zipfile.is_zipfile(io.BytesIO(content)):
        return content
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = [
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".csv")
        ]
        if len(candidates) != 1:
            raise ValueError("ZIP import must contain exactly one CSV file")
        member = candidates[0]
        if member.flag_bits & 0x1:
            raise ValueError("encrypted ZIP members are not supported")
        if member.file_size > settings.evidence_upload_max_bytes:
            raise ValueError(
                f"CSV in ZIP exceeds {settings.evidence_upload_max_bytes} uncompressed bytes"
            )
        return archive.read(member)


def _dataset_payload(settings: Settings, content: bytes, filename: str) -> tuple[bytes, str]:
    """Return one bounded structured dataset and its effective member name."""

    if not zipfile.is_zipfile(io.BytesIO(content)):
        return content, filename or "dataset.csv"
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        candidates = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith((".csv", ".json", ".jsonl"))
        ]
        if len(candidates) != 1:
            raise ValueError("ZIP import must contain exactly one CSV, JSON, or JSONL file")
        member = candidates[0]
        if member.flag_bits & 0x1:
            raise ValueError("encrypted ZIP members are not supported")
        if member.file_size > settings.evidence_upload_max_bytes:
            raise ValueError(
                f"dataset in ZIP exceeds {settings.evidence_upload_max_bytes} "
                "uncompressed bytes"
            )
        return archive.read(member), member.filename


def _dataset_rows(payload: bytes, filename: str) -> list[tuple[int, dict | object]]:
    """Decode CSV, JSON arrays/records, or JSON Lines into located row objects."""

    suffix = filename.lower().rsplit("/", 1)[-1]
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("dataset must be UTF-8 encoded") from exc
    if suffix.endswith(".jsonl"):
        rows: list[tuple[int, dict | object]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append((line_number, json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL on line {line_number}: {exc.msg}") from exc
        return rows
    if suffix.endswith(".json"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON dataset: {exc.msg}") from exc
        if isinstance(decoded, dict) and "records" in decoded:
            decoded = decoded["records"]
        if isinstance(decoded, dict):
            decoded = [decoded]
        if not isinstance(decoded, list):
            raise ValueError("JSON dataset must be an object, an array, or contain a records array")
        return list(enumerate(decoded, start=1))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV dataset must contain a header row")
    return list(enumerate(reader, start=2))


def _import_row(
    session: Session,
    document: SourceDocument,
    mapping: MappingSpec,
    row: dict,
    row_number: int,
    *,
    scope_snapshot: dict | None,
    pack_id: str | None,
    known_services: dict[str, str],
    known_archetypes: dict[str, str],
    observed_at: datetime | None,
    organisation_index: _OrganisationIndex,
) -> RowOutcome:
    name = mapping.value(row, "name")
    if not name:
        return RowOutcome(rejected_reason="missing name")
    if not _in_scope(scope_snapshot, mapping, row, pack_id=pack_id):
        return RowOutcome(out_of_scope=True)

    scheme = (mapping.value(row, "identifier_scheme") or "").lower() or None
    identifier = mapping.value(row, "identifier")
    locality = mapping.value(row, "locality")
    country = (mapping.value(row, "country") or "").upper() or None

    organisation = _find_organisation(
        session,
        scheme=scheme,
        identifier=identifier,
        name=name,
        locality=locality,
        country=country,
        index=organisation_index,
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

    def observe(
        predicate: str,
        value,
        confidence: float = 0.9,
        normalisation_status: str = NormalisationStatus.NORMALISED,
    ):
        return record_observation(
            session,
            document,
            predicate,
            value,
            subject_entity_id=organisation.id,
            extraction_method=ExtractionMethod.IMPORT,
            extraction_confidence=confidence,
            source_location=location_kwargs,
            observed_at=observed_at,
            normalisation_status=normalisation_status,
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
            observe(
                PREDICATE_DOMAIN,
                domain,
                confidence=0.0,
                normalisation_status=NormalisationStatus.REJECTED,
            )
    street_address = mapping.value(row, "street_address")
    if locality or mapping.value(row, "postcode") or country or street_address:
        observe(
            PREDICATE_LOCATION,
            {
                "street": street_address,
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
                address_lines=[street_address] if street_address else [],
                locality=locality,
                region=mapping.value(row, "region"),
                postal_code=mapping.value(row, "postcode"),
                country=country,
                location_type=LocationType.REGISTERED_ADDRESS,
            )
            entities.set_primary_location(session, organisation, location)
        elif street_address and organisation.primary_location is not None and not (
            organisation.primary_location.address_lines or []
        ):
            organisation.primary_location.address_lines = [street_address]

    email = mapping.value(row, "email")
    if email:
        email = email.strip().lower()
        if _EMAIL_RE.fullmatch(email):
            observation = observe(PREDICATE_EMAIL, email, confidence=0.85)
            local_part = email.split("@", 1)[0]
            entities.add_contact_point(
                session,
                organisation,
                ContactType.ROLE_EMAIL
                if local_part in _ROLE_LOCAL_PARTS
                else ContactType.GENERAL_EMAIL,
                email,
                role_based=local_part in _ROLE_LOCAL_PARTS,
                confidence=0.7,
                source_evidence_ids=[observation.id],
            )
        else:
            observe(
                PREDICATE_EMAIL,
                email,
                confidence=0.0,
                normalisation_status=NormalisationStatus.REJECTED,
            )
    phone = mapping.value(row, "phone")
    if phone:
        if phone_match_key(phone) is not None:
            observation = observe(PREDICATE_PHONE, phone, confidence=0.85)
            entities.add_contact_point(
                session,
                organisation,
                ContactType.PHONE,
                phone,
                confidence=0.7,
                source_evidence_ids=[observation.id],
            )
        else:
            observe(
                PREDICATE_PHONE,
                phone,
                confidence=0.0,
                normalisation_status=NormalisationStatus.REJECTED,
            )
    employees = mapping.value(row, "employees_band")
    if employees:
        valid_employee_band = employees in sizing.EMPLOYEE_BANDS
        observe(
            PREDICATE_EMPLOYEES,
            employees,
            confidence=0.85 if valid_employee_band else 0.0,
            normalisation_status=(
                NormalisationStatus.NORMALISED
                if valid_employee_band
                else NormalisationStatus.REJECTED
            ),
        )

    source = session.get(SourceDefinition, document.source_definition_id)
    source_category = source.source_category if source else None
    for claim in _split_claims(mapping.value(row, "service_claim")):
        if claim not in known_services or not pack_id:
            observe(
                PREDICATE_SERVICE_CLAIM,
                claim,
                confidence=0.0,
                normalisation_status=NormalisationStatus.REJECTED,
            )
            continue
        observation = observe(PREDICATE_SERVICE_CLAIM, claim, confidence=0.85)
        classifications.classify_from_observations(
            session,
            organisation.id,
            [observation],
            pack_id=pack_id,
            source_category=source_category,
            authority_tier=source.authority_tier if source else 7,
            known_service_ids=known_services,
        )
        capabilities.record_capability_evidence(
            session,
            organisation.id,
            pack_id=pack_id,
            capability_id=claim,
            capability_label=known_services[claim],
            observation_id=observation.id,
            source_definition_id=document.source_definition_id,
            source_category=source_category,
            authority_tier=source.authority_tier if source else 7,
            observed_at=observation.observed_at,
        )
    for claim in _split_claims(mapping.value(row, "archetype_claim")):
        if claim not in known_archetypes or not pack_id:
            observe(
                PREDICATE_ARCHETYPE_CLAIM,
                claim,
                confidence=0.0,
                normalisation_status=NormalisationStatus.REJECTED,
            )
            continue
        observation = observe(PREDICATE_ARCHETYPE_CLAIM, claim, confidence=0.85)
        classifications.classify_from_observations(
            session,
            organisation.id,
            [observation],
            pack_id=pack_id,
            source_category=source_category,
            authority_tier=source.authority_tier if source else 7,
            known_archetype_ids=known_archetypes,
        )

    organisation_index.add(organisation)
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
    pack_id = mapping.value({}, "pack_id")
    known_services, known_archetypes = _pack_vocabulary(pack_id, run.pack_snapshot)
    document = session.get(SourceDocument, run.source_document_id)
    if document is None:
        raise LookupError("import run has no stored dataset document")
    payload, effective_filename = _dataset_payload(
        settings, content, document.original_filename or "dataset.csv"
    )
    reader = _dataset_rows(payload, effective_filename)
    observed_at = _parse_coverage_date(run.coverage_date)
    organisation_index = _OrganisationIndex.build(session)

    touched: set[str] = set()
    rejected_samples: list[dict] = []
    counts = {"rows": 0, "imported": 0, "matched": 0, "out_of_scope": 0, "rejected": 0}
    for row_number, row in reader:
        counts["rows"] += 1
        if not isinstance(row, dict):
            counts["rejected"] += 1
            if len(rejected_samples) < _MAX_REJECTED_SAMPLES:
                rejected_samples.append(
                    {"row": row_number, "reason": "row is not an object"}
                )
            continue
        outcome = _import_row(
            session,
            document,
            mapping,
            row,
            row_number,
            scope_snapshot=run.scope_snapshot,
            pack_id=pack_id,
            known_services=known_services,
            known_archetypes=known_archetypes,
            observed_at=observed_at,
            organisation_index=organisation_index,
        )
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
    authority_tier: int = 5,
) -> BulkImportRun:
    """Store the file with provenance and queue the import job (§12.2)."""
    if not content:
        raise ValueError("dataset file is empty")
    if not dataset_name.strip():
        raise ValueError("dataset name is required")
    if not 1 <= authority_tier <= 7:
        raise ValueError("authority_tier must be between 1 and 7")
    _parse_coverage_date(coverage_date)
    pack_snapshot = _pack_snapshot(mapping.constants.get("pack_id"))
    scope_snapshot = _scope_snapshot(session)
    source = _bulk_source(session, dataset_name.strip(), publisher, authority_tier)
    effective_authority_tier = source.authority_tier
    document, digest = _store_dataset_document(
        session,
        settings,
        source,
        content,
        filename=filename,
        actor=actor,
        scope_snapshot=scope_snapshot,
        pack_snapshot=pack_snapshot,
        authority_tier=effective_authority_tier,
    )
    possible_duplicates = session.scalars(
        select(BulkImportRun).where(
            BulkImportRun.checksum == digest,
            BulkImportRun.status == ImportRunStatus.SUCCEEDED,
        )
    ).all()
    duplicate = next(
        (
            candidate
            for candidate in possible_duplicates
            if candidate.mapping == mapping.as_dict()
            and candidate.transformation_version == TRANSFORMATION_VERSION
        ),
        None,
    )
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
        scope_snapshot=scope_snapshot,
        pack_snapshot=pack_snapshot,
        authority_tier=effective_authority_tier,
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
        {
            "dataset": run.dataset_name,
            "checksum": digest[:12],
            "authority_tier": run.authority_tier,
            "scope_id": (run.scope_snapshot or {}).get("id"),
        },
    )
    return run


def load_run_content(settings: Settings, session: Session, run: BulkImportRun) -> bytes:
    if run.source_document_id is None:
        raise LookupError("import run has no stored dataset document")
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
