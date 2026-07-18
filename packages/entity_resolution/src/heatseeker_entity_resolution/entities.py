"""Creation and query helpers for the entity core.

Used by the API/UI now and by ingestion pipelines from M5 on. All writes go through
here so profile completeness and observation timestamps stay maintained in one place.
"""

from heatseeker_common.timeutil import utc_now
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from heatseeker_entity_resolution.models import (
    ContactPoint,
    ContactType,
    EntityProvenance,
    Location,
    OperationalUnit,
    Organisation,
    OrganisationDomain,
    OrganisationIdentifier,
    OrganisationStatus,
    OrganisationType,
    UnitType,
)
from heatseeker_entity_resolution.normalise import normalise_domain, normalise_identifier

_COMPLETENESS_FIELDS = 6  # legal_name, identifiers, domains, location, contacts, description


def refresh_profile_completeness(session: Session, organisation: Organisation) -> float:
    session.flush()
    present = sum(
        1
        for filled in (
            bool(organisation.legal_name),
            bool(organisation.identifiers),
            bool(organisation.domains),
            bool(organisation.primary_location_id),
            bool(organisation.contact_points),
            bool(organisation.description),
        )
        if filled
    )
    organisation.profile_completeness = round(present / _COMPLETENESS_FIELDS, 3)
    return organisation.profile_completeness


def create_organisation(
    session: Session,
    canonical_name: str,
    *,
    legal_name: str | None = None,
    trading_names: list[str] | None = None,
    organisation_type: str = OrganisationType.UNKNOWN,
    country_of_registration: str | None = None,
    description: str | None = None,
    provenance: str = EntityProvenance.MANUAL,
    identifiers: list[tuple[str, str]] | None = None,
    domains: list[str] | None = None,
) -> Organisation:
    name = canonical_name.strip()
    if not name:
        raise ValueError("canonical_name must not be blank")
    organisation = Organisation(
        canonical_name=name,
        legal_name=(legal_name or "").strip() or None,
        trading_names=trading_names or [],
        organisation_type=organisation_type,
        country_of_registration=country_of_registration,
        description=(description or "").strip() or None,
        provenance=provenance,
    )
    session.add(organisation)
    session.flush()
    for scheme, value in identifiers or []:
        add_identifier(session, organisation, scheme, value)
    for domain in domains or []:
        add_domain(session, organisation, domain)
    refresh_profile_completeness(session, organisation)
    return organisation


def add_identifier(
    session: Session,
    organisation: Organisation,
    scheme: str,
    value: str,
    *,
    country: str | None = None,
) -> OrganisationIdentifier:
    value = value.strip()
    if not value:
        raise ValueError("identifier value must not be blank")
    normalised = normalise_identifier(value)
    existing = session.execute(
        select(OrganisationIdentifier).where(
            OrganisationIdentifier.organisation_id == organisation.id,
            OrganisationIdentifier.scheme == scheme,
            OrganisationIdentifier.value_normalised == normalised,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_observed_at = utc_now()
        return existing
    identifier = OrganisationIdentifier(
        scheme=scheme,
        value=value,
        value_normalised=normalised,
        country=country,
    )
    # Append via the relationship so the loaded collection stays in sync.
    organisation.identifiers.append(identifier)
    session.flush()
    refresh_profile_completeness(session, organisation)
    return identifier


def add_domain(
    session: Session, organisation: Organisation, domain: str, *, is_primary: bool = False
) -> OrganisationDomain:
    host = normalise_domain(domain)
    if not host:
        raise ValueError(f"not a usable domain: {domain!r}")
    existing = session.execute(
        select(OrganisationDomain).where(
            OrganisationDomain.organisation_id == organisation.id,
            OrganisationDomain.domain == host,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.last_observed_at = utc_now()
        existing.is_primary = existing.is_primary or is_primary
        return existing
    record = OrganisationDomain(domain=host, is_primary=is_primary)
    organisation.domains.append(record)
    session.flush()
    refresh_profile_completeness(session, organisation)
    return record


def add_contact_point(
    session: Session,
    organisation: Organisation,
    contact_type: str,
    value: str,
    *,
    label: str | None = None,
    operational_unit_id: str | None = None,
    role_based: bool = False,
    confidence: float = 0.5,
    source_evidence_ids: list[str] | None = None,
) -> ContactPoint:
    value = value.strip()
    if not value:
        raise ValueError("contact value must not be blank")
    if operational_unit_id is not None:
        operational_unit = session.get(OperationalUnit, operational_unit_id)
        if operational_unit is None:
            raise ValueError(f"operational unit not found: {operational_unit_id}")
        if operational_unit.organisation_id != organisation.id:
            raise ValueError(
                "contact point and operational unit must belong to the same organisation"
            )
    # Same route re-observed: refresh and accumulate evidence instead of duplicating.
    for existing in organisation.contact_points:
        if (
            existing.contact_type == contact_type
            and existing.value.casefold() == value.casefold()
        ):
            existing.last_verified_at = utc_now()
            existing.confidence = max(existing.confidence, confidence)
            merged = set(existing.source_evidence_ids) | set(source_evidence_ids or [])
            existing.source_evidence_ids = sorted(merged)
            session.flush()
            return existing
    contact = ContactPoint(
        operational_unit_id=operational_unit_id,
        contact_type=contact_type,
        value=value,
        label=label,
        role_based=role_based or contact_type == ContactType.ROLE_EMAIL,
        confidence=confidence,
        source_evidence_ids=source_evidence_ids or [],
    )
    organisation.contact_points.append(contact)
    session.flush()
    refresh_profile_completeness(session, organisation)
    return contact


def add_location(session: Session, **fields) -> Location:
    location = Location(**fields)
    session.add(location)
    session.flush()
    return location


def set_primary_location(
    session: Session, organisation: Organisation, location: Location
) -> None:
    # Keep both the FK and the already-loaded relationship coherent. Profile research
    # reassesses gaps in the same transaction immediately after discovering an address.
    organisation.primary_location = location
    refresh_profile_completeness(session, organisation)


def add_unit(
    session: Session,
    organisation: Organisation,
    *,
    unit_type: str = UnitType.BRANCH,
    name: str | None = None,
    location_id: str | None = None,
    service_area: dict | None = None,
    active_status: str = "unknown",
) -> OperationalUnit:
    unit = OperationalUnit(
        unit_type=unit_type,
        name=name,
        location_id=location_id,
        service_area=service_area,
        active_status=active_status,
    )
    organisation.units.append(unit)
    session.flush()
    return unit


def get_organisation(session: Session, organisation_id: str) -> Organisation | None:
    return session.get(Organisation, organisation_id)


def list_organisations(
    session: Session,
    *,
    query: str | None = None,
    include_merged: bool = False,
    limit: int = 200,
) -> list[Organisation]:
    stmt = select(Organisation).order_by(Organisation.canonical_name).limit(limit)
    if not include_merged:
        stmt = stmt.where(Organisation.status != OrganisationStatus.MERGED)
    if query:
        needle = f"%{query.strip()}%"
        matching_ids = select(OrganisationIdentifier.organisation_id).where(
            OrganisationIdentifier.value_normalised.like(needle.upper())
        )
        matching_domains = select(OrganisationDomain.organisation_id).where(
            OrganisationDomain.domain.like(needle.lower())
        )
        stmt = stmt.where(
            or_(
                Organisation.canonical_name.like(needle),
                Organisation.legal_name.like(needle),
                Organisation.id.in_(matching_ids),
                Organisation.id.in_(matching_domains),
            )
        )
    return list(session.execute(stmt).scalars().unique())


def organisation_counts(session: Session) -> dict:
    total = session.execute(select(func.count(Organisation.id))).scalar_one()
    merged = session.execute(
        select(func.count(Organisation.id)).where(
            Organisation.status == OrganisationStatus.MERGED
        )
    ).scalar_one()
    return {"total": total, "merged": merged, "live": total - merged}
