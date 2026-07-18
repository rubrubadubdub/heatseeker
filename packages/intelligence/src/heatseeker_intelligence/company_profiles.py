"""Deterministic company-website profile collection — the AI-free enrichment path.

Given an organisation with a known domain, politely fetch its homepage plus a handful
of contact/about/location pages (robots honoured per URL, identified user-agent,
bounded bytes/pages), preserve every page as immutable evidence, distil to text, and
run the rule-based extractors (page_extraction) to produce observations → contact
points, addresses, branch units, pack-vocabulary service/system/archetype claims, and
in-house design capability evidence. Spec §41.19: quality results with AI disabled.
"""

import re
from urllib.parse import urlsplit

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
from heatseeker_entity_resolution.normalise import (
    normalise_domain,
    normalise_identifier,
    normalise_name,
    phone_match_key,
)
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
    PREDICATE_DOMAIN,
    PREDICATE_EMAIL,
    PREDICATE_IDENTIFIER,
    PREDICATE_LEGAL_NAME,
    PREDICATE_LOCATION,
    PREDICATE_PHONE,
    PREDICATE_SERVICE_CLAIM,
    record_observation,
)
from heatseeker_intelligence.page_extraction import (
    EXTRACTOR_VERSION,
    extract_page_metadata,
    extract_signals,
    is_role_email,
)
from heatseeker_intelligence.research_requirements import assess

PROFILE_FETCH_VERSION = "profile-fetch/0.2"
_PAGE_PURPOSES = (
    ("contact", "contact", "enquir", "quote", "estimate", "get-in-touch"),
    ("identity", "about", "company", "who-we-are", "privacy", "terms"),
    ("location", "location", "branch", "office", "yard", "depot"),
    ("services", "service", "capabilit", "solution", "what-we-do"),
    ("projects", "project", "portfolio", "case-stud", "our-work"),
    ("people", "team", "people", "management", "leadership"),
)
_MAX_PROFILE_PAGES = 12
_SOURCE_NAME = "Company websites (deterministic profile fetch)"


def entity_research_snapshot(organisation: Organisation, missing: tuple[str, ...]) -> dict:
    location = organisation.primary_location
    return {
        "organisation_id": organisation.id,
        "canonical_name": organisation.canonical_name,
        "legal_name": organisation.legal_name,
        "trading_names": list(organisation.trading_names or []),
        "identifiers": [
            {"scheme": item.scheme, "value": item.value} for item in organisation.identifiers
        ],
        "known_location": (
            {
                "locality": location.locality,
                "region": location.region,
                "country": location.country,
                "postcode": location.postal_code,
            }
            if location
            else None
        ),
        "known_domains": [item.domain for item in organisation.domains],
        "missing_required_fields": list(missing),
    }


def entity_research_queries(organisation: Organisation, missing: tuple[str, ...]) -> list[str]:
    """Stable, inspectable query plan ordered from strongest identity signal outward."""
    name = organisation.legal_name or organisation.canonical_name
    quoted = f'"{name}"'
    queries = [f"{quoted} official website"]
    queries.extend(f'{quoted} "{item.value}"' for item in organisation.identifiers)
    location = organisation.primary_location
    if location and (location.locality or location.region):
        place = " ".join(part for part in (location.locality, location.region) if part)
        queries.append(f"{quoted} {place}")
    if "public_contact_route" in missing or "specific_location" in missing:
        queries.append(f"{quoted} contact address phone email")
    if "business_description" in missing or "evidenced_relevance" in missing:
        queries.append(f"{quoted} services capabilities projects")
    return list(dict.fromkeys(queries))


def _legal_name_on_page(text: str, known_name: str) -> str | None:
    """Recover a legal suffix only when the page name matches the known entity core."""
    core = normalise_name(known_name)
    if not core:
        return None
    flexible_core = r"[\s&.,'()/-]+".join(re.escape(token) for token in core.split())
    match = re.search(
        rf"\b({flexible_core}[\s,]*(?:Pty\s+(?:Ltd|Limited)|NZ\s+Limited|Limited|Ltd))\b",
        text,
        re.IGNORECASE,
    )
    candidate = " ".join(match.group(1).split()) if match else None
    return candidate if candidate and normalise_name(candidate) == core else None


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


def verify_and_attach_domain(
    session: Session,
    settings: Settings,
    organisation_id: str,
    candidate_url: str,
    *,
    transport=None,
) -> dict:
    """Fetch an AI/search-proposed first-party page and attach only a proven identity.

    Search output is a lead, never evidence.  Acceptance is deterministic and requires
    corroborating content on the candidate host: exact legal wording, registration ID,
    known geography, or a same-domain business email in addition to the company name.
    """
    organisation = session.get(Organisation, organisation_id)
    if organisation is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    source = _profile_source(session)
    enforce = robots_enforced(source, settings)
    robots = RobotsCache(settings, transport)
    if enforce and not robots.allowed(candidate_url):
        return {"accepted": False, "reason": "robots_disallowed", "url": candidate_url}
    try:
        result = fetch_url(settings, candidate_url, transport=transport)
    except Exception as exc:
        return {"accepted": False, "reason": f"unreachable: {type(exc).__name__}"}
    if not result.content:
        return {"accepted": False, "reason": "empty_response", "url": candidate_url}

    final_url = str(result.final_url or candidate_url)
    host = normalise_domain(final_url)
    if not host:
        return {"accepted": False, "reason": "invalid_host", "url": final_url}
    document = _store_page(
        session,
        settings,
        source,
        final_url,
        result.content,
        result.content_type,
        str(RobotsStatus.ALLOWED if enforce else RobotsStatus.UNKNOWN),
        enforce,
    )
    text = html_to_text(result.content)
    name_key = normalise_name(organisation.legal_name or organisation.canonical_name)
    text_key = normalise_name(text)
    name_match = bool(name_key and name_key in text_key)
    score = 0.55 if name_match else 0.0
    reasons = ["normalised company name appears on page"] if name_match else []
    strong_identity_signal = False

    compact_text = normalise_identifier(text)
    identifier_hits = [
        item.value
        for item in organisation.identifiers
        if normalise_identifier(item.value) in compact_text
    ]
    if identifier_hits:
        score += 0.5
        strong_identity_signal = True
        reasons.append("registration identifier appears on page")

    raw_folded = " ".join(text.casefold().split())
    exact_names = [organisation.canonical_name, organisation.legal_name]
    if any(value and " ".join(value.casefold().split()) in raw_folded for value in exact_names):
        score += 0.2
        strong_identity_signal = True
        reasons.append("exact recorded name appears on page")

    location = organisation.primary_location
    location_terms = [
        value.casefold()
        for value in (
            location.locality if location else None,
            location.region if location else None,
            location.postal_code if location else None,
        )
        if value and len(value) >= 3
    ]
    if location_terms and any(term in raw_folded for term in location_terms):
        score += 0.15
        strong_identity_signal = True
        reasons.append("known geography appears on page")

    signals = extract_signals(text)
    if any(normalise_domain(email.split("@", 1)[1]) == host for email in signals.emails):
        score += 0.15
        reasons.append("same-domain business email appears on page")

    score = round(min(score, 1.0), 3)
    if score < 0.7 or not strong_identity_signal:
        return {
            "accepted": False,
            "reason": "identity_threshold_not_met",
            "score": score,
            "signals": reasons,
            "url": final_url,
        }
    observation = record_observation(
        session,
        document,
        PREDICATE_DOMAIN,
        host,
        subject_entity_id=organisation.id,
        extraction_method=ExtractionMethod.DETERMINISTIC,
        extraction_confidence=score,
        source_location={
            "url": final_url,
            "verification_rule": "candidate-domain/0.1",
            "signals": reasons,
        },
    )
    entities.add_domain(session, organisation, host, is_primary=True)
    facts.reconcile(session, organisation.id, PREDICATE_DOMAIN)
    return {
        "accepted": True,
        "domain": host,
        "score": score,
        "signals": reasons,
        "observation_id": observation.id,
        "url": final_url,
    }


def _subpage_urls(base_url: str, html: str, missing: tuple[str, ...] = ()) -> list[str]:
    """Rank useful internal links by the fields still missing, then by page purpose."""
    host = (urlsplit(base_url).hostname or "").removeprefix("www.")
    metadata = extract_page_metadata(html, base_url)
    wanted = {
        "official_domain": {"identity"},
        "stable_identity": {"identity"},
        "specific_location": {"location", "contact"},
        "public_contact_route": {"contact", "people"},
        "business_description": {"identity", "services"},
        "evidenced_relevance": {"services", "projects"},
    }
    priority_purposes = set().union(*(wanted.get(field, set()) for field in missing))
    ranked: list[tuple[int, int, str]] = []
    for order, (absolute, anchor) in enumerate(metadata.links):
        parts = urlsplit(absolute)
        candidate_host = (parts.hostname or "").removeprefix("www.")
        if candidate_host != host:
            continue
        haystack = f"{parts.path} {anchor}".casefold()
        purposes = {
            purpose
            for purpose, *terms in _PAGE_PURPOSES
            if any(term in haystack for term in terms)
        }
        if not purposes:
            continue
        rank = 0 if purposes & priority_purposes else 1
        ranked.append((rank, order, absolute))
    return [url for _rank, _order, url in sorted(ranked)]


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
    description_recorded = bool(organisation.description)

    while queue and summary["pages"] < _MAX_PROFILE_PAGES:
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
        current_report = assess(session, organisation)
        queue.extend(
            candidate
            for candidate in _subpage_urls(url, html, current_report.missing)
            if candidate not in fetched and candidate not in queue
        )

        text = html_to_text(result.content)
        metadata = extract_page_metadata(html, str(result.final_url or url))
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

        for scheme, value in signals.identifiers:
            observe(
                PREDICATE_IDENTIFIER,
                {"scheme": scheme, "value": value},
                conf=0.75,
            )
            entities.add_identifier(session, organisation, scheme, value)
            summary["identifiers"] = summary.get("identifiers", 0) + 1
        if not organisation.legal_name:
            legal_name = _legal_name_on_page(text, organisation.canonical_name)
            if legal_name:
                observe(PREDICATE_LEGAL_NAME, legal_name, conf=0.75)
                organisation.legal_name = legal_name
                entities.refresh_profile_completeness(session, organisation)
                summary["legal_names"] = summary.get("legal_names", 0) + 1

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
        if metadata.contact_form_url:
            observation = observe("contact_form", metadata.contact_form_url, conf=0.7)
            entities.add_contact_point(
                session,
                organisation,
                ContactType.CONTACT_FORM,
                metadata.contact_form_url,
                confidence=0.7,
                source_evidence_ids=[observation.id],
            )
            summary["contact_forms"] = summary.get("contact_forms", 0) + 1
        if metadata.description and not description_recorded:
            observation = observe("description", metadata.description, conf=0.7)
            organisation.description = metadata.description
            entities.refresh_profile_completeness(session, organisation)
            description_recorded = True
            summary["descriptions"] = summary.get("descriptions", 0) + 1
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
    completion = assess(session, organisation)
    summary["completion"] = {
        "complete": completion.complete,
        "score": completion.score,
        "missing": list(completion.missing),
        "signature": completion.signature,
    }
    audit.record(
        session, "profile-fetch", "profile.fetched", "organisation", organisation.id, summary
    )
    return summary
