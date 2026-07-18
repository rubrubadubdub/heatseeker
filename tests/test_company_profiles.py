"""Deterministic profile engine: extraction rules + AI-free end-to-end fetch (§41.19)."""

import httpx
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_intelligence.company_profiles import (
    entity_research_queries,
    fetch_and_extract,
    verify_and_attach_domain,
)
from heatseeker_intelligence.models import CapabilityAssignment, Observation
from heatseeker_intelligence.page_extraction import extract_signals
from heatseeker_lead_intelligence import service
from heatseeker_lead_intelligence.scoring import score_organisation
from sqlalchemy import select

SAMPLE = """
# Acme Scaffolding — Commercial & Industrial Access

Established 1985. Our in-house drafting team designs every scaffold we erect.
We hire and install Kwik Stage and Cuplok systems across Queensland.

Contact us: info@acmescaffolding.com.au | estimating@acmescaffolding.com.au
Phone (07) 3333 1111 or 1300 722 999
Head office: 12 Gantry Road, Acacia Ridge, QLD 4110
Gold Coast yard: 4/8 Spine Street, Molendinar, QLD 4214
"""


def test_extract_signals_rules():
    signals = extract_signals(
        SAMPLE,
        services={"scaffold_design": "Scaffold design", "drafting_2d": "2D drafting"},
        systems={"kwik_stage": "Kwik Stage", "cuplok": "Cuplok", "layher": "Layher"},
        archetypes={"scaffold_contractor": "Scaffold contractor"},
    )
    assert signals.emails == [
        "info@acmescaffolding.com.au", "estimating@acmescaffolding.com.au",
    ]
    assert any("3333 1111" in phone for phone in signals.phones)
    assert any(phone.startswith("1300") for phone in signals.phones)
    assert [a["postcode"] for a in signals.addresses] == ["4110", "4214"]
    assert signals.addresses[0]["street"] == "12 Gantry Road"
    assert {hit for hit, _ in signals.system_hits} == {"kwik_stage", "cuplok"}
    assert signals.inhouse_design_phrases  # "in-house drafting team"
    # No fabrication: nothing matched for absent vocab items.
    assert all(hit != "layher" for hit, _ in signals.system_hits)


def _mock_site_transport():
    homepage = (
        "<html><body><a href='/contact-us'>Contact</a>"
        "<p>Acme Scaffolding hires Kwik Stage systems. "
        "Our in-house drafting team designs every job.</p></body></html>"
    )
    contact = (
        "<html><body><h1>Contact</h1><p>Email info@acmescaffolding.com.au "
        "Phone (07) 3333 1111</p><p>12 Gantry Road, Acacia Ridge, QLD 4110</p>"
        "<p>Yard: 4/8 Spine Street, Molendinar, QLD 4214</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if path == "/":
            return httpx.Response(200, html=homepage)
        if path == "/contact-us":
            return httpx.Response(200, html=contact)
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def test_fetch_and_extract_end_to_end_without_ai(engine, settings):
    with session_scope(engine) as session:
        org = entities.create_organisation(
            session, "Acme Scaffolding Pty Ltd", domains=["acmescaffolding.com.au"]
        )
        org_id = org.id

    with session_scope(engine) as session:
        summary = fetch_and_extract(
            session, settings, org_id, transport=_mock_site_transport()
        )
        assert summary["status"] == "ok"
        assert summary["pages"] == 2  # homepage + discovered contact page
        assert summary["emails"] >= 1 and summary["phones"] >= 1

    with session_scope(engine) as session:
        org = entities.get_organisation(session, org_id)
        routes = {(c.contact_type, c.value) for c in org.contact_points}
        assert ("general_email", "info@acmescaffolding.com.au") in routes
        # First address became HQ; second became a branch unit → group size resolves.
        assert org.primary_location.address_lines == ["12 Gantry Road"]
        assert len(org.units) == 1
        # Self-claimed in-house design became a CLAIMED capability with evidence.
        capability = session.execute(
            select(CapabilityAssignment).where(
                CapabilityAssignment.organisation_id == org_id,
                CapabilityAssignment.capability_id == "scaffold_design",
            )
        ).scalar_one()
        assert capability.capability_status == "claimed"
        # Systems recorded as observations with page provenance.
        systems = list(
            session.execute(
                select(Observation).where(
                    Observation.subject_entity_id == org_id,
                    Observation.predicate == "uses_system",
                )
            ).scalars()
        )
        assert {o.object_value for o in systems} == {"kwikstage"}
        assert all(o.source_location.get("url") for o in systems)
        # Every observation is deterministic — zero AI anywhere in this path.
        methods = {
            o.extraction_method
            for o in session.execute(
                select(Observation).where(Observation.subject_entity_id == org_id)
            ).scalars()
        }
        assert methods == {"deterministic"}


def test_claimed_design_counts_against_need_gap(engine, settings):
    with session_scope(engine) as session:
        offering = service.create_offering(
            session,
            "Design outsourcing",
            need_gap_capability_ids=["scaffold_design"],
        )
        org = entities.create_organisation(
            session, "Acme Scaffolding Pty Ltd", domains=["acmescaffolding.com.au"]
        )
        fetch_and_extract(session, settings, org.id, transport=_mock_site_transport())
        score = score_organisation(session, org, offering)
        # Self-claimed in-house design → secondary target, not disqualified.
        assert score.components["need_likelihood"] == 0.45
        assert any("secondary target" in r["text"] for r in score.reasons)


def test_robots_disallow_blocks_page(engine, settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, html="<html><body>hi</body></html>")

    with session_scope(engine) as session:
        org = entities.create_organisation(
            session, "Blocked Co", domains=["blocked.example"]
        )
        summary = fetch_and_extract(
            session, settings, org.id, transport=httpx.MockTransport(handler)
        )
        assert summary["pages"] == 0
        assert summary["blocked"] >= 1  # robots honoured — nothing fetched


def test_candidate_domain_requires_page_identity_corroboration(engine, settings):
    exact_page = (
        "<html><body><h1>Rovera Scaffolding Pty Limited</h1>"
        "<p>ACN 095 140 819</p><p>Email info@roverascaffolding.com.au</p></body></html>"
    )
    wrong_page = (
        "<html><body><h1>Rovera Scaffolding (QLD) Pty Ltd</h1>"
        "<p>info@roveraqld.com.au</p></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        html = exact_page if request.url.host == "roverascaffolding.com.au" else wrong_page
        return httpx.Response(200, html=html)

    transport = httpx.MockTransport(handler)
    with session_scope(engine) as session:
        org = entities.create_organisation(
            session, "Rovera Scaffolding Pty Limited", identifiers=[("acn", "095140819")]
        )
        rejected = verify_and_attach_domain(
            session, settings, org.id, "https://roveraqld.com.au/", transport=transport
        )
        accepted = verify_and_attach_domain(
            session,
            settings,
            org.id,
            "https://roverascaffolding.com.au/contact",
            transport=transport,
        )
        assert rejected["accepted"] is False
        assert accepted["accepted"] is True
        assert [domain.domain for domain in org.domains] == ["roverascaffolding.com.au"]
        queries = entity_research_queries(org, ("public_contact_route",))
        assert any("095140819" in query for query in queries)
        assert any("contact address phone email" in query for query in queries)


def test_profile_fetch_follows_targeted_pages_and_records_description(engine, settings):
    pages = {
        "/": (
            "<html><head><meta name='description' content='Acme delivers complex "
            "commercial and industrial scaffolding throughout Queensland.'></head><body>"
            "<a href='/about-us'>About</a><a href='/services'>Capabilities</a>"
            "<a href='/contact'>Contact</a></body></html>"
        ),
        "/about-us": "<html><body><p>Acme Scaffolding Pty Ltd</p></body></html>",
        "/services": "<html><body><p>Scaffold erection and Kwikstage hire.</p></body></html>",
        "/contact": (
            "<html><body><form><input name='email'></form>"
            "<p>12 Gantry Road, Acacia Ridge, QLD 4110</p></body></html>"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, html=pages.get(request.url.path, "missing"))

    with session_scope(engine) as session:
        org = entities.create_organisation(
            session,
            "Acme Scaffolding Pty Ltd",
            identifiers=[("abn", "51824753556")],
            domains=["acme.example"],
        )
        summary = fetch_and_extract(
            session, settings, org.id, transport=httpx.MockTransport(handler)
        )
        assert summary["pages"] == 4
        assert org.description.startswith("Acme delivers complex")
        assert org.legal_name == "Acme Scaffolding Pty Ltd"
        assert any(contact.contact_type == "contact_form" for contact in org.contact_points)
        assert org.primary_location.postal_code == "4110"
