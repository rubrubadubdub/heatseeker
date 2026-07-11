"""Idempotent industry-pack seed synchronisation into the canonical source registry."""

from __future__ import annotations

import re
from dataclasses import dataclass

from heatseeker_common import audit
from heatseeker_common.timeutil import utc_now
from heatseeker_core_domain.geography import parse_jurisdiction
from heatseeker_industry_packs.loader import LoadedPack
from heatseeker_industry_packs.schemas import SeedCoverage, SeedSource, SeedSourceV2
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from heatseeker_source_registry.identity import (
    IdentitySpec,
    SourceIdentityConflict,
    attach_identity,
    pack_seed_identity,
    resolve_identities,
    shared_source_identity,
    url_identity,
)
from heatseeker_source_registry.models import (
    RobotsStatus,
    SourceCoverage,
    SourceDefinition,
    SourceLifecycle,
)
from heatseeker_source_registry.targeting import (
    CoverageSpec,
    TargetSpec,
    disable_coverage,
    upsert_coverage,
)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return (slug or "source")[:80]


def _seed_key(seed: SeedSource | SeedSourceV2) -> str:
    return seed.key if isinstance(seed, SeedSourceV2) else _slug(seed.name)


def _identities(pack_id: str, seed: SeedSource | SeedSourceV2) -> tuple[IdentitySpec, ...]:
    identities = [pack_seed_identity(pack_id, _seed_key(seed))]
    if isinstance(seed, SeedSourceV2) and seed.source_key:
        identities.append(shared_source_identity(seed.source_key))
    identities.append(url_identity(seed.url))
    return tuple(identities)


def _seed_regions(seed: SeedSource | SeedSourceV2) -> list[str]:
    if isinstance(seed, SeedSource):
        return parse_jurisdiction(seed.jurisdiction, validate=True)
    return sorted({code for coverage in seed.coverages for code in coverage.region_codes})


def _new_source(pack_id: str, seed: SeedSource | SeedSourceV2) -> SourceDefinition:
    regions = _seed_regions(seed)
    if isinstance(seed, SeedSourceV2):
        lifecycle = (
            SourceLifecycle.PROPOSED
            if seed.lifecycle_status == SourceLifecycle.PROPOSED
            else SourceLifecycle.CANDIDATE
        )
        return SourceDefinition(
            name=seed.name,
            source_category=seed.category,
            base_url=seed.url,
            jurisdiction=", ".join(regions) or None,
            geo_codes=regions,
            language=seed.language,
            access_method=seed.access,
            authority_tier=seed.authority_tier,
            expected_update_frequency=seed.expected_update_frequency,
            lifecycle_status=lifecycle,
            authentication_type=seed.authentication_type,
            rate_limit_policy=seed.rate_limit_policy,
            collection_scope=seed.collection_scope,
            parser_profile=seed.parser_profile,
            origin="pack_seed",
            pack_id=pack_id,
            notes=seed.notes,
        )
    return SourceDefinition(
        name=seed.name,
        source_category=seed.category,
        base_url=seed.url,
        jurisdiction=seed.jurisdiction,
        geo_codes=regions,
        access_method=seed.access,
        authority_tier=seed.authority_tier,
        lifecycle_status=SourceLifecycle.CANDIDATE,
        origin="pack_seed",
        pack_id=pack_id,
        notes=seed.notes,
    )


def _owned_seed_values(seed: SeedSource | SeedSourceV2) -> dict[str, object]:
    regions = _seed_regions(seed)
    values: dict[str, object] = {
        "name": seed.name,
        "source_category": seed.category,
        "base_url": seed.url,
        "jurisdiction": (
            seed.jurisdiction if isinstance(seed, SeedSource) else ", ".join(regions) or None
        ),
        "geo_codes": regions,
        "access_method": seed.access,
        "authority_tier": seed.authority_tier,
        "notes": seed.notes,
    }
    if isinstance(seed, SeedSourceV2):
        values.update(
            language=seed.language,
            expected_update_frequency=seed.expected_update_frequency,
            authentication_type=seed.authentication_type,
            rate_limit_policy=seed.rate_limit_policy,
            collection_scope=seed.collection_scope,
            parser_profile=seed.parser_profile,
        )
    return values


def _update_owned_source(
    source: SourceDefinition, pack_id: str, seed: SeedSource | SeedSourceV2
) -> bool:
    """Update only the pack that originally owns global metadata.

    Other packs add contextual coverage and aliases without last-writer-wins changes to
    policy, lifecycle, health, or canonical metadata.
    """
    if source.origin != "pack_seed" or source.pack_id != pack_id:
        return False
    changed = False
    old_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
    old_access_method = source.access_method
    for field_name, value in _owned_seed_values(seed).items():
        if getattr(source, field_name) != value:
            setattr(source, field_name, value)
            changed = True
    new_policy_url = (source.collection_scope or {}).get("endpoint_url") or source.base_url
    if old_policy_url != new_policy_url:
        # A robots decision is path-specific and cannot follow an endpoint change.
        source.robots_status = RobotsStatus.UNKNOWN
        source.robots_checked_at = None
    if old_access_method != source.access_method:
        source.robots_status = RobotsStatus.UNKNOWN
        source.robots_checked_at = None
        for coverage in source.coverages:
            coverage.robots_status = RobotsStatus.UNKNOWN
            coverage.robots_checked_at = None
            coverage.updated_at = utc_now()
    if changed:
        source.updated_at = utc_now()
    return changed


def _coverage_targets(
    pack_id: str, coverage: SeedCoverage | None, regions: list[str]
) -> tuple[TargetSpec, ...]:
    industries = coverage.industry_ids if coverage and coverage.industry_ids else [pack_id]
    region_codes = coverage.region_codes if coverage else regions
    targets = [TargetSpec("industry", industry_id) for industry_id in industries]
    targets.extend(TargetSpec("region", code, match_mode="hierarchical") for code in region_codes)
    if coverage:
        targets.extend(
            TargetSpec(
                target.dimension,
                target.target_key,
                target.target_label,
                target.polarity,
                target.match_mode,
            )
            for target in coverage.targets
        )
    return tuple(targets)


def _coverage_specs(pack: LoadedPack, seed: SeedSource | SeedSourceV2) -> tuple[CoverageSpec, ...]:
    seed_key = _seed_key(seed)
    if isinstance(seed, SeedSource):
        regions = _seed_regions(seed)
        return (
            CoverageSpec(
                coverage_key=f"pack:{pack.pack_id}:{seed_key}:default",
                name=seed.name,
                targets=_coverage_targets(pack.pack_id, None, regions),
                authority_tier_override=seed.authority_tier,
                origin="pack_seed",
                origin_pack_id=pack.pack_id,
                origin_pack_version=pack.version,
                origin_pack_hash=pack.content_hash,
            ),
        )
    specs = []
    for coverage in seed.coverages:
        specs.append(
            CoverageSpec(
                coverage_key=f"pack:{pack.pack_id}:{seed.key}:{coverage.key}",
                name=coverage.name or f"{seed.name} — {coverage.key}",
                description=coverage.description,
                lifecycle_status=coverage.lifecycle_status,
                priority=coverage.priority,
                relevance=coverage.relevance_score,
                confidence=coverage.confidence_score,
                authority_tier_override=(coverage.authority_tier_override or seed.authority_tier),
                collection_scope_override=coverage.collection_scope,
                parser_profile_override=coverage.parser_profile,
                valid_from=coverage.valid_from,
                valid_to=coverage.valid_to,
                targets=_coverage_targets(pack.pack_id, coverage, []),
                origin="pack_seed",
                origin_pack_id=pack.pack_id,
                origin_pack_version=pack.version,
                origin_pack_hash=pack.content_hash,
            )
        )
    return tuple(specs)


@dataclass(slots=True)
class _Counters:
    sources_created: int = 0
    sources_updated: int = 0
    sources_unchanged: int = 0
    identities_created: int = 0
    coverages_created: int = 0
    coverages_updated: int = 0
    coverages_unchanged: int = 0
    coverages_disabled: int = 0

    def changed(self) -> bool:
        return any(
            (
                self.sources_created,
                self.sources_updated,
                self.identities_created,
                self.coverages_created,
                self.coverages_updated,
                self.coverages_disabled,
            )
        )


def sync_pack_seeds(session: Session, pack: LoadedPack, actor: str = "system") -> dict:
    """Atomically reconcile one pack's source identities and coverage profiles.

    Existing lifecycle/terms decisions are never overwritten. Removed pack profiles are
    disabled, while source definitions and evidence remain intact.
    """
    seeds_file = pack.files.get("sources/seed_sources.yaml")
    if seeds_file is None:
        return {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "total": 0,
            "processed": 0,
            "conflicts": [],
        }

    resolved: dict[str, SourceDefinition | None] = {}
    conflicts: list[dict[str, str]] = []
    for seed in seeds_file.sources:
        try:
            resolved[_seed_key(seed)] = resolve_identities(session, _identities(pack.pack_id, seed))
        except SourceIdentityConflict as exc:
            conflicts.append({"seed_key": _seed_key(seed), "error": str(exc)})
    if conflicts:
        audit.record(
            session,
            actor,
            "sources.seed_sync_conflict",
            "industry_pack",
            pack.pack_id,
            {
                "pack_version": pack.version,
                "pack_hash": pack.content_hash,
                "conflicts": conflicts,
            },
        )
        return {
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "total": len(seeds_file.sources),
            "processed": 0,
            "conflicts": conflicts,
        }

    counters = _Counters()
    desired_coverage_keys: set[str] = set()
    existing_seed_count = 0
    for seed in seeds_file.sources:
        seed_key = _seed_key(seed)
        source = resolved[seed_key]
        if source is None:
            source = _new_source(pack.pack_id, seed)
            session.add(source)
            session.flush()
            counters.sources_created += 1
        else:
            existing_seed_count += 1
            if _update_owned_source(source, pack.pack_id, seed):
                counters.sources_updated += 1
            else:
                counters.sources_unchanged += 1

        identity_specs = _identities(pack.pack_id, seed)
        primary_types = (
            {"source_key"}
            if any(identity.identity_type == "source_key" for identity in identity_specs)
            else {"url"}
        )
        for identity in identity_specs:
            _, created = attach_identity(
                session,
                source,
                identity,
                origin="pack_seed",
                is_primary=identity.identity_type in primary_types,
            )
            counters.identities_created += int(created)

        for spec in _coverage_specs(pack, seed):
            desired_coverage_keys.add(spec.coverage_key)
            _, outcome = upsert_coverage(session, source, spec, actor=actor)
            if outcome == "created":
                counters.coverages_created += 1
            elif outcome == "updated":
                counters.coverages_updated += 1
            else:
                counters.coverages_unchanged += 1

    pack_coverages = list(
        session.scalars(
            select(SourceCoverage)
            .where(
                SourceCoverage.origin_pack_id == pack.pack_id,
                SourceCoverage.origin.in_(["pack_seed", "migration"]),
            )
            .options(selectinload(SourceCoverage.targets))
        )
    )
    for coverage in pack_coverages:
        if coverage.coverage_key not in desired_coverage_keys:
            outcome = disable_coverage(
                session,
                coverage,
                actor=actor,
                reason="removed from industry-pack seed file",
            )
            counters.coverages_disabled += int(outcome == "disabled")

    detail = {
        "pack_version": pack.version,
        "pack_hash": pack.content_hash,
        "sources_created": counters.sources_created,
        "sources_updated": counters.sources_updated,
        "sources_unchanged": counters.sources_unchanged,
        "identities_created": counters.identities_created,
        "coverages_created": counters.coverages_created,
        "coverages_updated": counters.coverages_updated,
        "coverages_unchanged": counters.coverages_unchanged,
        "coverages_disabled": counters.coverages_disabled,
    }
    if counters.changed():
        audit.record(
            session,
            actor,
            "sources.seeds_synced",
            "industry_pack",
            pack.pack_id,
            detail,
        )

    # ``updated`` retains the M2 meaning (an existing seed was reconciled) for callers
    # that predate the detailed counters. Actual row mutations are in sources_updated.
    return {
        "created": counters.sources_created,
        "updated": existing_seed_count,
        "unchanged": counters.sources_unchanged,
        "total": len(seeds_file.sources),
        "processed": len(seeds_file.sources),
        "conflicts": [],
        **detail,
    }
