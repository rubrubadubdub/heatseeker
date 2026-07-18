"""Deterministic profile-completion contract for autonomous company research.

The entity model deliberately permits partial records.  Lead delivery cannot: a name
and a weak classification are a research candidate, not an actionable lead.  This
module defines the small, industry-neutral set of public facts HeatSeeker must obtain
before a company can leave the research queue.
"""

import hashlib
import json
from dataclasses import dataclass

from heatseeker_entity_resolution.models import Organisation
from heatseeker_entity_resolution.resolution import merge_group
from sqlalchemy.orm import Session

from heatseeker_intelligence.capabilities import capabilities_for
from heatseeker_intelligence.classifications import classifications_for

REQUIREMENTS_VERSION = "profile-requirements/0.1"


@dataclass(frozen=True, slots=True)
class CompletionReport:
    complete: bool
    score: float
    present: tuple[str, ...]
    missing: tuple[str, ...]
    signature: str


def assess(session: Session, organisation: Organisation) -> CompletionReport:
    """Assess export-critical fields without treating missing values as false facts."""
    group = merge_group(session, organisation.id)
    group_ids = [member.id for member in group]
    classifications = classifications_for(session, group_ids)
    capabilities = capabilities_for(session, group_ids)
    fields = {
        # A legal/registry identity is needed to distinguish same-name businesses.
        "stable_identity": any(member.identifiers or member.legal_name for member in group),
        "official_domain": any(member.domains for member in group),
        "specific_location": any(
            member.primary_location
            and member.primary_location.country
            and (member.primary_location.region or member.primary_location.locality)
            for member in group
        ),
        "public_contact_route": any(
            contact.public_business_contact
            for member in group
            for contact in member.contact_points
        ),
        "business_description": any(member.description for member in group),
        "evidenced_relevance": bool(
            capabilities
            or any(
                assignment.assignment_type != "rejected" and assignment.confidence >= 0.5
                for assignment in classifications
            )
        ),
    }
    present = tuple(name for name, value in fields.items() if value)
    missing = tuple(name for name, value in fields.items() if not value)
    payload = {
        "version": REQUIREMENTS_VERSION,
        "organisation_id": organisation.id,
        "present": present,
        "missing": missing,
    }
    signature = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return CompletionReport(
        complete=not missing,
        score=round(len(present) / len(fields), 3),
        present=present,
        missing=missing,
        signature=signature,
    )
