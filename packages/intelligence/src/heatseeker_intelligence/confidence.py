"""Field-level confidence composition (spec §17).

Deterministic and fully inspectable: every component is computed by code, stored on the
assertion, and surfaced in the profile. The formula is replaceable (§17.2) — this
version is multiplicative with bounded corroboration uplift — but the components are
mandatory.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from heatseeker_common.timeutil import utc_now

RULE_VERSION = "confidence/0.2"

# Freshness half-lives in days, per predicate (§17.4). None = no decay (historical
# events stay true). Unlisted predicates use DEFAULT_HALF_LIFE_DAYS.
FRESHNESS_HALF_LIFE_DAYS: dict[str, float | None] = {
    "registration_status": 180.0,
    "registration_identifier": 3650.0,
    "canonical_name": 1825.0,
    "legal_name": 1825.0,
    "trading_name": 1095.0,
    "phone": 365.0,
    "email": 365.0,
    "website_domain": 730.0,
    "location": 730.0,
    "service_claim": 730.0,
    "archetype_claim": 730.0,
    "employee_count_band": 540.0,
    "description": 540.0,
    "project_participation": None,
}
DEFAULT_HALF_LIFE_DAYS = 540.0

# Authority: tier 1 (official registry) → 1.0 down to tier 7 → 0.52 (§10.2 tiers).
# Question-relative preferences (§17.3): these source categories get a bonus for these
# predicates, because the *kind* of source matters more than the global tier alone.
_PREFERRED_CATEGORIES: dict[str, tuple[str, ...]] = {
    "registration_status": ("government_registry", "regulator", "bulk_dataset"),
    "registration_identifier": ("government_registry", "regulator", "bulk_dataset"),
    "service_claim": ("company_website",),
    "archetype_claim": ("company_website",),
    "phone": ("company_website",),
    "email": ("company_website",),
}

STALE_THRESHOLD = 0.3


def authority_score(authority_tier: int, source_category: str | None, predicate: str) -> float:
    base = max(0.0, min(1.0, 1.0 - 0.08 * (max(1, authority_tier) - 1)))
    if source_category and source_category in _PREFERRED_CATEGORIES.get(predicate, ()):
        base = min(1.0, base + 0.1)
    return round(base, 3)


def freshness_score(predicate: str, observed_at: datetime, now: datetime | None = None) -> float:
    half_life = FRESHNESS_HALF_LIFE_DAYS.get(predicate, DEFAULT_HALF_LIFE_DAYS)
    if half_life is None:
        return 1.0
    age_days = max(0.0, ((now or utc_now()) - observed_at).total_seconds() / 86400.0)
    return round(math.pow(0.5, age_days / half_life), 3)


def corroboration_score(independent_source_count: int) -> float:
    """Bounded uplift for independent agreement; a single source is neutral (1.0)."""
    if independent_source_count <= 1:
        return 1.0
    return round(min(1.2, 1.0 + 0.08 * (independent_source_count - 1)), 3)


def contradiction_score(supporting: int, contradicting: int) -> float:
    """1.0 with no conflict, shrinking as the contested share grows (§17.6)."""
    total = supporting + contradicting
    if total == 0 or contradicting == 0:
        return 1.0
    return round(max(0.4, 1.0 - 0.6 * (contradicting / total)), 3)


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    authority: float
    extraction: float
    match: float
    freshness: float
    corroboration: float
    contradiction: float

    @property
    def final(self) -> float:
        product = (
            self.authority
            * self.extraction
            * self.match
            * self.freshness
            * self.corroboration
            * self.contradiction
        )
        return round(max(0.0, min(1.0, product)), 3)

    def as_dict(self) -> dict:
        return {
            "authority": self.authority,
            "extraction": self.extraction,
            "match": self.match,
            "freshness": self.freshness,
            "corroboration": self.corroboration,
            "contradiction": self.contradiction,
            "final": self.final,
        }


def vocabulary(
    final: float,
    *,
    conflicted: bool = False,
    stale: bool = False,
    human_verified: bool = False,
    has_evidence: bool = True,
) -> str:
    """Human-readable confidence vocabulary (§17.7)."""
    if not has_evidence:
        return "unknown"
    if conflicted:
        return "conflicted"
    if stale:
        return "stale"
    if human_verified:
        return "verified"
    if final >= 0.75:
        return "high"
    if final >= 0.5:
        return "moderate"
    if final >= 0.3:
        return "low"
    return "speculative"
