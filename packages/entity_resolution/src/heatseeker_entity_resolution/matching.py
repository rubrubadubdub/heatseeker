"""Deterministic match scoring and blocked duplicate scanning (spec §14.2-§14.3).

Scoring never merges anything: it creates/updates `entity_match_candidate` rows for a
human to resolve. Signals are recorded per pair so every score is explainable.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations

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
from heatseeker_entity_resolution.normalise import name_tokens, normalise_name, phone_match_key

# Weights: a shared registered identifier is decisive; domains and exact legal names are
# strong; fuzzy names alone can only ever reach the review queue.
_W_IDENTIFIER = 1.0
_W_DOMAIN = 0.9
_W_NAME_EXACT = 0.85
_W_NAME_FUZZY_MAX = 0.6
_W_PHONE = 0.15
_W_LOCALITY = 0.05
_CONFLICT_PENALTY = 0.3

_FUZZY_RATIO_FLOOR = 0.75
_TOKEN_JACCARD_FLOOR = 0.5

EXACT_THRESHOLD = 0.98
HIGH_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.45

_MAX_BLOCK_SIZE = 50  # guard against degenerate blocks ("scaffolding" as a name token)


@dataclass
class OrgFeatures:
    organisation_id: str
    name_norm: str
    alt_name_norms: set[str] = field(default_factory=set)
    identifier_keys: set[tuple[str, str]] = field(default_factory=set)  # (scheme, value)
    identifier_schemes: dict[str, set[str]] = field(default_factory=dict)
    domains: set[str] = field(default_factory=set)
    phone_keys: set[str] = field(default_factory=set)
    locality_keys: set[str] = field(default_factory=set)


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
    location = organisation.primary_location
    if location is not None and location.locality:
        key = location.locality.casefold().strip()
        if location.postal_code:
            key = f"{key}|{location.postal_code.strip()}"
        features.locality_keys.add(key)
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
            score = max(ratio if ratio >= _FUZZY_RATIO_FLOOR else 0.0,
                        jaccard if jaccard >= _TOKEN_JACCARD_FLOOR else 0.0)
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
    for name in {features.name_norm, *features.alt_name_norms}:
        tokens = name.split()
        if tokens:
            keys.add(f"name:{tokens[0]}")
    return keys


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
            )
        )
        .scalars()
        .unique()
        .all()
    )
    features_by_id = {org.id: build_features(org) for org in organisations}

    blocks: dict[str, list[str]] = {}
    for org_id, features in features_by_id.items():
        for key in _blocking_keys(features):
            blocks.setdefault(key, []).append(org_id)

    pairs: set[tuple[str, str]] = set()
    oversized_blocks = 0
    for members in blocks.values():
        if len(members) < 2:
            continue
        if len(members) > _MAX_BLOCK_SIZE:
            oversized_blocks += 1
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
        if row is None:
            session.add(
                EntityMatchCandidate(
                    organisation_a_id=a_id,
                    organisation_b_id=b_id,
                    match_state=state,
                    score=score,
                    signals=signals,
                    conflict_count=conflicts,
                )
            )
            created += 1
        elif row.resolution is None:
            row.match_state = state
            row.score = score
            row.signals = signals
            row.conflict_count = conflicts
            row.updated_at = utc_now()
            updated += 1
    session.flush()
    return {
        "organisations": len(organisations),
        "pairs_scored": len(pairs),
        "candidates_created": created,
        "candidates_updated": updated,
        "oversized_blocks_skipped": oversized_blocks,
    }
