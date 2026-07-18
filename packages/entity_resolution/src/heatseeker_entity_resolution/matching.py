"""Deterministic match scoring and blocked duplicate scanning (spec §14.2-§14.3).

Scoring never merges anything: it creates/updates `entity_match_candidate` rows for a
human to resolve. Signals are recorded per pair so every score is explainable.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations

from heatseeker_common.public_profiles import try_social_profile_url
from heatseeker_common.timeutil import utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from heatseeker_entity_resolution.models import (
    ContactType,
    EntityMatchCandidate,
    MatchState,
    Organisation,
    OrganisationStatus,
)
from heatseeker_entity_resolution.normalise import (
    blocking_name_tokens,
    email_domain,
    name_tokens,
    normalise_address,
    normalise_name,
    phone_match_key,
)

# Weights: a shared registered identifier is decisive; domains and exact legal names are
# strong; fuzzy names alone can only ever reach the review queue.
_W_IDENTIFIER = 1.0
_W_DOMAIN = 0.9
_W_NAME_EXACT = 0.85
_W_NAME_FUZZY_MAX = 0.6
_W_PHONE = 0.15
_W_LOCALITY = 0.05
_W_EMAIL_DOMAIN = 0.55
_W_ADDRESS = 0.65
_W_SOCIAL_PROFILE = 0.75
_CONFLICT_PENALTY = 0.3

_FUZZY_RATIO_FLOOR = 0.75
_TOKEN_JACCARD_FLOOR = 0.5

EXACT_THRESHOLD = 0.98
HIGH_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.45

_MAX_WEAK_BLOCK_SIZE = 50  # guard against generic tokens (for example "scaffolding")


@dataclass
class OrgFeatures:
    organisation_id: str
    name_norm: str
    alt_name_norms: set[str] = field(default_factory=set)
    identifier_keys: set[tuple[str, str]] = field(default_factory=set)  # (scheme, value)
    identifier_schemes: dict[str, set[str]] = field(default_factory=dict)
    domains: set[str] = field(default_factory=set)
    phone_keys: set[str] = field(default_factory=set)
    email_domain_keys: set[str] = field(default_factory=set)
    address_keys: set[str] = field(default_factory=set)
    locality_keys: set[str] = field(default_factory=set)
    social_profile_keys: set[str] = field(default_factory=set)


def build_features(organisation: Organisation) -> OrgFeatures:
    features = OrgFeatures(
        organisation_id=organisation.id,
        name_norm=normalise_name(organisation.canonical_name),
    )
    for alt in [organisation.legal_name, *(organisation.trading_names or [])]:
        if alt:
            normalised = normalise_name(alt)
            if normalised and normalised != features.name_norm:
                features.alt_name_norms.add(normalised)
    for identifier in organisation.identifiers:
        if not identifier.is_current:
            continue
        features.identifier_keys.add((identifier.scheme, identifier.value_normalised))
        features.identifier_schemes.setdefault(identifier.scheme, set()).add(
            identifier.value_normalised
        )
    features.domains = {d.domain for d in organisation.domains}
    for contact in organisation.contact_points:
        if contact.contact_type == ContactType.PHONE:
            key = phone_match_key(contact.value)
            if key:
                features.phone_keys.add(key)
        elif contact.contact_type in {ContactType.GENERAL_EMAIL, ContactType.ROLE_EMAIL}:
            domain = email_domain(contact.value)
            if domain:
                features.email_domain_keys.add(domain)
        elif contact.contact_type == ContactType.SOCIAL_PROFILE:
            profile = try_social_profile_url(contact.value)
            if profile is not None:
                features.social_profile_keys.add(profile.url)
    location = organisation.primary_location
    if location is not None:
        if location.locality:
            key = location.locality.casefold().strip()
            if location.postal_code:
                key = f"{key}|{location.postal_code.strip()}"
            features.locality_keys.add(key)
        address_parts = list(location.address_lines or [])
        if address_parts and location.postal_code:
            address_parts.append(location.postal_code)
        address = normalise_address(
            [
                *address_parts,
                location.locality,
                location.region,
                location.country,
            ]
        ) if address_parts else None
        if address:
            features.address_keys.add(address)
    return features


def _name_similarity(a: OrgFeatures, b: OrgFeatures) -> tuple[float, str]:
    """Best similarity across canonical/legal/trading names; (ratio, matched-name note)."""
    best = 0.0
    note = ""
    for name_a in {a.name_norm, *a.alt_name_norms}:
        for name_b in {b.name_norm, *b.alt_name_norms}:
            if not name_a or not name_b:
                continue
            if name_a == name_b:
                return 1.0, name_a
            tokens_a, tokens_b = name_tokens(name_a), name_tokens(name_b)
            union = tokens_a | tokens_b
            jaccard = len(tokens_a & tokens_b) / len(union) if union else 0.0
            ratio = SequenceMatcher(None, name_a, name_b).ratio()
            score = max(
                ratio if ratio >= _FUZZY_RATIO_FLOOR else 0.0,
                jaccard if jaccard >= _TOKEN_JACCARD_FLOOR else 0.0,
            )
            if score > best:
                best, note = score, f"{name_a} ~ {name_b}"
    return best, note


def score_pair(a: OrgFeatures, b: OrgFeatures) -> tuple[str, float, list[dict], int]:
    """Return (match_state, score, signals, conflict_count) for a pair of organisations."""
    signals: list[dict] = []
    conflicts = 0
    contributions: list[float] = []
    deterministic = False

    shared_ids = a.identifier_keys & b.identifier_keys
    if shared_ids:
        deterministic = True
        contributions.append(_W_IDENTIFIER)
        signals.append(
            {
                "signal": "shared_identifier",
                "detail": sorted(f"{scheme}:{value}" for scheme, value in shared_ids),
                "weight": _W_IDENTIFIER,
            }
        )
    for scheme in set(a.identifier_schemes) & set(b.identifier_schemes):
        values_a, values_b = a.identifier_schemes[scheme], b.identifier_schemes[scheme]
        if values_a and values_b and not (values_a & values_b):
            conflicts += 1
            signals.append(
                {
                    "signal": "conflicting_identifier",
                    "detail": [scheme, sorted(values_a), sorted(values_b)],
                    "weight": -_CONFLICT_PENALTY,
                }
            )

    shared_domains = a.domains & b.domains
    if shared_domains:
        contributions.append(_W_DOMAIN)
        signals.append(
            {"signal": "shared_domain", "detail": sorted(shared_domains), "weight": _W_DOMAIN}
        )

    similarity, matched = _name_similarity(a, b)
    if similarity >= 1.0:
        contributions.append(_W_NAME_EXACT)
        signals.append({"signal": "name_exact", "detail": matched, "weight": _W_NAME_EXACT})
    elif similarity > 0.0:
        weight = round(_W_NAME_FUZZY_MAX * similarity, 3)
        contributions.append(weight)
        signals.append({"signal": "name_fuzzy", "detail": matched, "weight": weight})

    if a.phone_keys & b.phone_keys:
        contributions.append(_W_PHONE)
        signals.append(
            {
                "signal": "shared_phone",
                "detail": sorted(a.phone_keys & b.phone_keys),
                "weight": _W_PHONE,
            }
        )
    if a.email_domain_keys & b.email_domain_keys:
        contributions.append(_W_EMAIL_DOMAIN)
        signals.append(
            {
                "signal": "shared_email_domain",
                "detail": sorted(a.email_domain_keys & b.email_domain_keys),
                "weight": _W_EMAIL_DOMAIN,
            }
        )
    if a.address_keys & b.address_keys:
        contributions.append(_W_ADDRESS)
        signals.append(
            {
                "signal": "shared_address",
                "detail": sorted(a.address_keys & b.address_keys),
                "weight": _W_ADDRESS,
            }
        )
    if a.social_profile_keys & b.social_profile_keys:
        contributions.append(_W_SOCIAL_PROFILE)
        signals.append(
            {
                "signal": "shared_social_profile",
                "detail": sorted(a.social_profile_keys & b.social_profile_keys),
                "weight": _W_SOCIAL_PROFILE,
            }
        )
    if a.locality_keys & b.locality_keys:
        contributions.append(_W_LOCALITY)
        signals.append(
            {
                "signal": "shared_locality",
                "detail": sorted(a.locality_keys & b.locality_keys),
                "weight": _W_LOCALITY,
            }
        )

    # Noisy-or combination keeps the score in [0, 1] while rewarding corroboration.
    score = 1.0
    for weight in contributions:
        score *= 1.0 - weight
    score = 1.0 - score
    score = max(0.0, round(score - conflicts * _CONFLICT_PENALTY, 3))

    if not contributions:
        return MatchState.UNRESOLVED, 0.0, signals, conflicts
    if deterministic and not conflicts and score >= EXACT_THRESHOLD:
        state = MatchState.EXACT
    elif score >= HIGH_THRESHOLD and not conflicts:
        state = MatchState.HIGH_CONFIDENCE_PROBABLE
    elif score >= REVIEW_THRESHOLD:
        state = MatchState.POSSIBLE_REVIEW
    else:
        state = MatchState.UNRESOLVED
    return state, score, signals, conflicts


def _blocking_keys(features: OrgFeatures) -> set[str]:
    keys = {f"id:{scheme}:{value}" for scheme, value in features.identifier_keys}
    keys.update(f"dom:{domain}" for domain in features.domains)
    keys.update(f"phone:{key}" for key in features.phone_keys)
    keys.update(f"email:{domain}" for domain in features.email_domain_keys)
    keys.update(f"address:{address}" for address in features.address_keys)
    keys.update(f"social:{profile}" for profile in features.social_profile_keys)
    for name in {features.name_norm, *features.alt_name_norms}:
        if name:
            keys.add(f"name-exact:{name}")
        keys.update(f"name-token:{token}" for token in blocking_name_tokens(name))
    return keys


def _priority_dimensions(
    a: Organisation,
    b: Organisation,
    *,
    score: float,
    conflicts: int,
    signals: list[dict],
    commercial_importance: float = 0.0,
) -> tuple[float, float, float]:
    """Return downstream impact, ease of resolution, and combined queue priority.

    Commercial importance is an explicit input reserved for M5 data. We do not infer it
    from profile completeness, because that would turn missing data into a negative fact.
    """
    commercial_importance = max(0.0, min(1.0, commercial_importance))
    attached_rows = sum(
        len(collection)
        for organisation in (a, b)
        for collection in (
            organisation.identifiers,
            organisation.domains,
            organisation.contact_points,
            organisation.units,
        )
    )
    downstream_impact = round(min(1.0, attached_rows / 10.0), 3)
    signal_names = {signal["signal"] for signal in signals}
    if "shared_identifier" in signal_names:
        ease = 1.0
    elif signal_names & {
        "shared_domain",
        "name_exact",
        "shared_address",
        "shared_social_profile",
    }:
        ease = 0.8
    else:
        ease = max(0.1, score - (0.15 * conflicts))
    ease_of_resolution = round(min(1.0, ease), 3)
    conflict_priority = min(1.0, conflicts / 3.0)
    priority = round(
        (score * 0.55)
        + (commercial_importance * 0.1)
        + (downstream_impact * 0.15)
        + (conflict_priority * 0.1)
        + (ease_of_resolution * 0.1),
        3,
    )
    return downstream_impact, ease_of_resolution, priority


def scan_for_duplicates(session: Session, *, min_score: float = REVIEW_THRESHOLD) -> dict:
    """Score candidate pairs from shared blocking keys; upsert the review queue.

    Idempotent: pairs a human already resolved are left untouched; unresolved pairs get
    refreshed scores. Merged organisations are excluded (their survivors represent them).
    """
    organisations = (
        session.execute(
            select(Organisation)
            .where(Organisation.status != OrganisationStatus.MERGED)
            .options(
                selectinload(Organisation.identifiers),
                selectinload(Organisation.domains),
                selectinload(Organisation.contact_points),
                selectinload(Organisation.units),
            )
        )
        .scalars()
        .unique()
        .all()
    )
    features_by_id = {org.id: build_features(org) for org in organisations}
    organisations_by_id = {org.id: org for org in organisations}

    blocks: dict[str, list[str]] = {}
    for org_id, features in features_by_id.items():
        for key in _blocking_keys(features):
            blocks.setdefault(key, []).append(org_id)

    pairs: set[tuple[str, str]] = set()
    oversized_blocks_skipped = oversized_blocks_limited = 0
    for key, members in blocks.items():
        if len(members) < 2:
            continue
        if len(members) > _MAX_WEAK_BLOCK_SIZE:
            if key.startswith("name-token:"):
                oversized_blocks_skipped += 1
                continue
            # Strong evidence blocks (same identifier/domain/phone/address/exact name)
            # still need coverage. A deterministic star compares every member with an
            # anchor without allowing a corrupt block to create O(n^2) pairs.
            oversized_blocks_limited += 1
            ordered = sorted(members)
            pairs.update((ordered[0], member) for member in ordered[1:])
            continue
        for a, b in combinations(sorted(members), 2):
            pairs.add((a, b))

    existing = {
        (row.organisation_a_id, row.organisation_b_id): row
        for row in session.execute(select(EntityMatchCandidate)).scalars()
    }

    created = updated = 0
    for a_id, b_id in sorted(pairs):
        state, score, signals, conflicts = score_pair(features_by_id[a_id], features_by_id[b_id])
        if score < min_score:
            continue
        row = existing.get((a_id, b_id))
        commercial_importance = row.commercial_importance if row is not None else 0.0
        downstream_impact, ease_of_resolution, priority_score = _priority_dimensions(
            organisations_by_id[a_id],
            organisations_by_id[b_id],
            score=score,
            conflicts=conflicts,
            signals=signals,
            commercial_importance=commercial_importance,
        )
        if row is None:
            session.add(
                EntityMatchCandidate(
                    organisation_a_id=a_id,
                    organisation_b_id=b_id,
                    match_state=state,
                    score=score,
                    signals=signals,
                    conflict_count=conflicts,
                    downstream_impact=downstream_impact,
                    ease_of_resolution=ease_of_resolution,
                    priority_score=priority_score,
                )
            )
            created += 1
        elif row.resolution is None:
            row.match_state = state
            row.score = score
            row.signals = signals
            row.conflict_count = conflicts
            row.downstream_impact = downstream_impact
            row.ease_of_resolution = ease_of_resolution
            row.priority_score = priority_score
            row.updated_at = utc_now()
            updated += 1
    session.flush()
    return {
        "organisations": len(organisations),
        "pairs_scored": len(pairs),
        "candidates_created": created,
        "candidates_updated": updated,
        "oversized_blocks_skipped": oversized_blocks_skipped,
        "oversized_blocks_limited": oversized_blocks_limited,
    }
