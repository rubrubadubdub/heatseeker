"""Deterministic company-name → website discovery (no search engine, no AI).

The missing first hop that lets registry-only companies (name + ACN, no website) enter
the enrichment chain. Generates plausible Australian domain candidates from the
company name, then hands each to the rigorous verifier (verify_and_attach_domain),
which only attaches a domain when the fetched page deterministically proves identity.
Guessing is free and AI-free; acceptance stays evidence-gated, so a wrong guess is
simply rejected, never recorded.
"""

import re

from heatseeker_entity_resolution.models import Organisation
from heatseeker_entity_resolution.normalise import normalise_name
from sqlalchemy.orm import Session

from heatseeker_intelligence.company_profiles import verify_and_attach_domain

# Australian business TLDs first (target market), then global fallbacks.
_TLDS = ("com.au", "net.au", "au", "com")
_MAX_CANDIDATES = 12


def candidate_domains(name: str) -> list[str]:
    """Ordered, de-duplicated candidate hostnames for a company name."""
    normalised = normalise_name(name)  # legal-suffix-stripped, lowercased, punctuation-free
    tokens = [t for t in normalised.split() if t]
    if not tokens:
        return []

    full = "".join(tokens)
    hyphen = "-".join(tokens)
    first = tokens[0]
    first_two = "".join(tokens[:2]) if len(tokens) >= 2 else full

    # Slug candidates in rough order of likelihood.
    slugs: list[str] = [full, hyphen, first_two]
    slugs.append(first)

    seen: set[str] = set()
    hosts: list[str] = []
    for slug in slugs:
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        if len(slug) < 3:
            continue
        for tld in _TLDS:
            host = f"{slug}.{tld}"
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts[:_MAX_CANDIDATES]


def discover_domain(
    session: Session,
    settings,
    organisation_id: str,
    *,
    transport=None,
    max_candidates: int = _MAX_CANDIDATES,
) -> dict:
    """Try generated candidates until the verifier accepts one (or all fail)."""
    organisation = session.get(Organisation, organisation_id)
    if organisation is None:
        raise LookupError(f"organisation not found: {organisation_id}")
    if organisation.domains:
        return {"status": "already_has_domain", "organisation": organisation.canonical_name}

    tried: list[str] = []
    for host in candidate_domains(organisation.canonical_name)[:max_candidates]:
        url = f"https://{host}/"
        tried.append(host)
        outcome = verify_and_attach_domain(
            session, settings, organisation_id, url, transport=transport
        )
        if outcome.get("accepted"):
            return {
                "status": "discovered",
                "organisation": organisation.canonical_name,
                "domain": outcome.get("domain", host),
                "identity_score": outcome.get("score"),
                "signals": outcome.get("signals"),
                "candidates_tried": len(tried),
            }
    return {
        "status": "not_found",
        "organisation": organisation.canonical_name,
        "candidates_tried": len(tried),
    }
