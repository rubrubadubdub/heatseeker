"""Deterministic company-website profile collection — the AI-free enrichment path.

Given an organisation with a known domain, politely fetch its homepage plus a handful
of contact/about/location pages (robots honoured per URL, identified user-agent,
bounded bytes/pages), preserve every page as immutable evidence, distil to text, and
run the rule-based extractors (page_extraction) to produce observations → contact
points, addresses, branch units, pack-vocabulary service/system/archetype claims, and
in-house design capability evidence. Spec §41.19: quality results with AI disabled.
"""

import re
from urllib.parse import urljoin, urlsplit

from heatseeker_common import audit
from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.models import (
    ContactType,
    LocationType,
    Organisation,
    UnitType,
)
from heatseeker_entity_resolution.normalise import phone_match_key
from heatseeker_source_registry.crawler import RobotsCache
from heatseeker_source_registry.distill import html_to_text
from heatseeker_source_registry.fetch import fetch_url
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceDefinition,
    SourceDocument,
    SourceLifecycle,
)
from heatseeker_source_registry.policy import robots_enforced
from heatseeker_source_registry.rawstore import store_bytes
from sqlalchemy import select
from sqlalchemy.orm import Session

from heatseeker_intelligence import capabilities, classifications, facts, gaps, sizing
from heatseeker_intelligence.discovery import _pack_vocabulary
from heatseeker_intelligence.models import ExtractionMethod
from heatseeker_intelligence.observations import (
    PREDICATE_EMAIL,
    PREDICATE_LOCATION,
    PREDICATE_PHONE,
    PREDICATE_SERVICE_CLAIM,
    record_observation,
)
from heatseeker_intelligence.page_extraction import (
    EXTRACTOR_VERSION,
    extract_signals,
    is_role_email,
)

PROFILE_FETCH_VERSION = "profile-fetch/0.1"
_SUBPAGE_HINT = re.compile(
    r'href=["\']([^"\']*(?:contact|about|location|branch|our-team|services)[^"\']*)["\']',
    re.IGNORECASE,
)
_MAX_SUBPAGES = 3
_SOURCE_NAME = "Company websites (deterministic profile fetch)"


def _profile_source(session: Session) -> SourceDefinition:
    source = session.scalars(
        select(SourceDefinition).where(SourceDefinition.name == _SOURCE_NAME)
    ).first()
    if source is not None:
        return source
    source = SourceDefinition(
        name=_SOURCE_NAME,
        source_category="company_website",
        access_method="html",
        authority_tier=4,  # first-party self-description (§17.3)
        lifecycle_status=SourceLifecycle.ACTIVE,
        origin="user",
        notes="Shared source for per-organisation website profile fetches.",
    )
    session.add(source)
    session.flush()
    return source


def _store_page(
    session: Session,
    settings: Settings,
    source: SourceDefinition,
    url: str,
    payload: bytes,
    content_type: str | None,
    robots_status: str,
    enforce: bool,
) -> SourceDocument:
    rel_path, digest = store_bytes(settings, payload, content_type)
    existing = session.scalars(
        select(SourceDocument).where(
            SourceDocument.source_definition_id == source.id,
            SourceDocument.source_url == url,
            SourceDocument.content_hash == digest,
        )
    ).first()
    if existing is not None:
        existing.last_seen_at = utc_now()
        existing.retrieval_count += 1
        return existing
    document = SourceDocument(
        source_definition_id=source.id,
        source_url=url,
        content_hash=digest,
        content_type=content_type,
        size_bytes=len(payload),
        raw_storage_path=rel_path,
        access_policy_snapshot={
            "acquisition": "profile_fetch",
            "robots_status": robots_status,
            "robots_enforced": enforce,
            "user_agent": settings.crawler_user_agent,
        },
        targeting_snapshot={"schema_version": 1, "mode": "profile_fetch"},
        collector_version=PROFILE_FETCH_VERSION,
        parser_version=EXTRACTOR_VERSION,
    )
    session.add(document)
    session.flush()
    return document


def _subpage_urls(base_url: str, html: str) -> list[str]:
    host = urlsplit(base_url).netloc
    found: list[str] = []
    for match in _SUBPAGE_HINT.finditer(html):
        absolute = urljoin(base_url, match.group(1))
        parts = urlsplit(absolute)
        if parts.scheme in ("http", "https") and parts.netloc == host:
            cleaned = absolute.split("#", 1)[0]
            if cleaned not in found and cleaned != base_url:
                found.append(cleaned)
        if len(found) >= _MAX_SUBPAGES:
            break
    return found


def fetch_and_extract(
    session: Session,
    settings: Settings,
    organisation_id: str,
    *,
    transport=None,
    pack_id: str = "scaffolding_anz",
) -> dict:
    """Fetch one organisation's site and turn pages into evidence-backed profile data."""
    organisation = session.get(Organisation, organisation_id)
    if organisation is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    if not organisation.domains:
        return {"organisation": organisation.canonical_name, "status": "no_domain"}

    source = _profile_source(session)
    enforce = robots_enforced(source, settings)
    robots = RobotsCache(settings, transport)
    services, archetypes = _pack_vocabulary(pack_id)
    try:
        from heatseeker_industry_packs.loader import default_packs_root, load_pack

        systems_file = load_pack(default_packs_root() / pack_id).files.get(
            "products_systems.yaml"
        )
        systems = {
            item.id: [item.name, *item.synonyms]
            for item in (systems_file.systems if systems_file else [])
        }
    except Exception:  # pack unavailable — extraction still works without vocab
        systems = {}

    summary = {
        "organisation": organisation.canonical_name,
        "pages": 0,
        "blocked": 0,
        "emails": 0,
        "phones": 0,
        "addresses": 0,
        "claims": 0,
        "status": "ok",
    }
    base_url = f"https://{organisation.domains[0].domain}/"
    queue = [base_url]
    fetched: set[str] = set()
    homepage_html: str | None = None

    while queue and summary["pages"] < 1 + _MAX_SUBPAGES:
        url = queue.pop(0)
        if url in fetched:
            continue
        fetched.add(url)
        if enforce and not robots.allowed(url):
            summary["blocked"] += 1
            continue
        try:
            result = fetch_url(settings, url, transport=transport)
        except Exception:
            if summary["pages"] == 0 and not queue:
                summary["status"] = "unreachable"
            continue
        if result.not_modified or not result.content:
            continue
        summary["pages"] += 1
        robots_state = (
            RobotsStatus.ALLOWED if enforce else RobotsStatus.UNKNOWN
        )
        document = _store_page(
            session, settings, source, str(result.final_url or url),
            result.content, result.content_type, str(robots_state), enforce,
        )
        html = result.content.decode("utf-8", errors="replace")
        if homepage_html is None:
            homepage_html = html
            queue.extend(_subpage_urls(url, html))

        text = html_to_text(result.content)
        signals = extract_signals(
            text, services=services, systems=systems, archetypes=archetypes
        )
        provenance = {"url": str(result.final_url or url), "extractor": EXTRACTOR_VERSION}

        def observe(predicate, value, conf=0.7, doc=document, prov=provenance):
            return record_observation(
                session, doc, predicate, value,
                subject_entity_id=organisation.id,
                extraction_method=ExtractionMethod.DETERMINISTIC,
                extraction_confidence=conf,
                source_location=prov,
            )

        for email in signals.emails:
            observation = observe(PREDICATE_EMAIL, email)
            role = is_role_email(email)
            entities.add_contact_point(
                session, organisation,
                ContactType.ROLE_EMAIL if role else ContactType.GENERAL_EMAIL,
                email, role_based=role, confidence=0.7,
                source_evidence_ids=[observation.id],
            )
            summary["emails"] += 1
        for phone in signals.phones:
            if not phone_match_key(phone):
                continue
            observation = observe(PREDICATE_PHONE, phone)
            entities.add_contact_point(
                session, organisation, ContactType.PHONE, phone,
                confidence=0.7, source_evidence_ids=[observation.id],
            )
            summary["phones"] += 1
        existing_units = {(unit.name or "").casefold() for unit in organisation.units}
        for index, address in enumerate(signals.addresses):
            observe(PREDICATE_LOCATION, {**address, "country": "AU"}, conf=0.65)
            summary["addresses"] += 1
            if index == 0 and organisation.primary_location_id is None:
                location = entities.add_location(
                    session,
                    address_lines=[address["street"]],
                    locality=address["locality"],
                    region=address["state"],
                    postal_code=address["postcode"],
                    country="AU",
                    location_type=LocationType.OFFICE,
                )
                entities.set_primary_location(session, organisation, location)
            elif index > 0:
                label = f"{address['locality']}, {address['state']}".casefold()
                if label in existing_units:
                    continue
                location = entities.add_location(
                    session,
                    address_lines=[address["street"]],
                    locality=address["locality"],
                    region=address["state"],
                    postal_code=address["postcode"],
                    country="AU",
                    location_type=LocationType.YARD,
                )
                entities.add_unit(
                    session, organisation, unit_type=UnitType.BRANCH,
                    name=f"{address['locality']}, {address['state']}",
                    location_id=location.id, active_status="active",
                )
                existing_units.add(label)

        design_hits = list(signals.inhouse_design_phrases)
        service_ids = {hit for hit, _text in signals.service_hits}
        if design_hits:
            service_ids.add("scaffold_design")
        for service_id in sorted(service_ids):
            if service_id not in services and service_id != "scaffold_design":
                continue
            observation = observe(PREDICATE_SERVICE_CLAIM, service_id, conf=0.7)
            summary["claims"] += 1
            classifications.classify_from_observations(
                session, organisation.id, [observation],
                pack_id=pack_id, source_category=source.source_category,
                authority_tier=source.authority_tier, known_service_ids=services,
            )
            capabilities.record_capability_evidence(
                session, organisation.id,
                pack_id=pack_id, capability_id=service_id,
                capability_label=services.get(service_id, service_id),
                observation_id=observation.id,
                source_definition_id=source.id,
                source_category=source.source_category,
                authority_tier=source.authority_tier,
                observed_at=observation.observed_at,
            )
        for archetype_id, _text in signals.archetype_hits:
            observation = observe("archetype_claim", archetype_id, conf=0.6)
            summary["claims"] += 1
            classifications.classify_from_observations(
                session, organisation.id, [observation],
                pack_id=pack_id, source_category=source.source_category,
                authority_tier=source.authority_tier, known_archetype_ids=archetypes,
            )
        for system_id, _text in signals.system_hits:
            observe("uses_system", system_id, conf=0.7)

    if summary["pages"]:
        facts.reconcile_all(session, organisation.id)
        sizing.estimate_sizes(session, organisation.id)
        gaps.generate_for(session, organisation.id)
    audit.record(
        session, "profile-fetch", "profile.fetched", "organisation", organisation.id, summary
    )
    return summary
