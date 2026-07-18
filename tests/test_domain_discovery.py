"""Deterministic name → website discovery (AI-free, evidence-gated attach)."""

import httpx
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_intelligence.domain_discovery import candidate_domains, discover_domain


def test_candidate_domains_generation():
    hosts = candidate_domains("Rovera Scaffolding Pty Limited")
    # Legal suffix dropped; AU TLDs first; real site is among the first guesses.
    assert "roverascaffolding.com.au" in hosts
    assert hosts.index("roverascaffolding.com.au") < hosts.index("roverascaffolding.com")
    assert "rovera-scaffolding.com.au" in hosts
    assert candidate_domains("   ") == []


def _site(host: str, *, body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != host:
            # Simulate NXDOMAIN for every wrong guess.
            raise httpx.ConnectError("name not resolved")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, html=body)

    return httpx.MockTransport(handler)


def test_discover_attaches_only_verified_domain(engine, settings):
    with session_scope(engine) as session:
        org = entities.create_organisation(
            session, "Rovera Scaffolding Pty Limited", identifiers=[("acn", "095140819")]
        )
        org_id = org.id

    # The real site corroborates identity: exact name + ACN + a same-domain email.
    body = (
        "<html><body><h1>Rovera Scaffolding Pty Limited</h1>"
        "<p>ACN 095 140 819. Canberra's scaffolding experts since 1980.</p>"
        "<p>Email info@roverascaffolding.com.au phone (02) 6260 1422</p></body></html>"
    )
    with session_scope(engine) as session:
        outcome = discover_domain(
            session, settings, org_id,
            transport=_site("roverascaffolding.com.au", body=body),
        )
        assert outcome["status"] == "discovered"
        assert outcome["domain"] == "roverascaffolding.com.au"
        assert outcome["identity_score"] >= 0.7

    with session_scope(engine) as session:
        org = entities.get_organisation(session, org_id)
        assert [d.domain for d in org.domains] == ["roverascaffolding.com.au"]


def test_discover_rejects_unrelated_site(engine, settings):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Rovera Scaffolding Pty Limited")
        org_id = org.id

    # A parked/unrelated page at a guessed domain — name absent → never attached.
    body = "<html><body><h1>Premium Domains For Sale</h1></body></html>"
    with session_scope(engine) as session:
        outcome = discover_domain(
            session, settings, org_id,
            transport=_site("roverascaffolding.com.au", body=body),
        )
        assert outcome["status"] == "not_found"

    with session_scope(engine) as session:
        org = entities.get_organisation(session, org_id)
        assert org.domains == []  # nothing fabricated
