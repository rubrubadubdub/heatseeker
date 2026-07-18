"""Pydantic schemas for industry-pack YAML files (ADR-0005, spec §8.2).

Every pack file declares `schema: <name>/v<N>` and validates strictly
(extra keys are errors) so typos are caught at load time — M1 acceptance:
"Pack validation catches invalid configuration."
"""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_GEO_CODE_RE = re.compile(
    r"^(?:GLOBAL|ANZ|APAC|NORTH_AMERICA|EUROPE|[A-Z]{2}(?:-[A-Z0-9][A-Z0-9_]{0,62})*)$"
)

AccessMethod = Literal["api", "bulk", "rss", "sitemap", "html", "rendered", "manual"]
SeedSourceStatus = Literal["proposed", "candidate"]
TermsStatusValue = Literal["unreviewed", "approved", "unclear", "prohibited"]
TargetPolarity = Literal["include", "exclude"]
TargetMatchMode = Literal["exact", "hierarchical", "covers", "within"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_id(value: str) -> str:
    if len(value) > 100 or not _ID_RE.match(value):
        raise ValueError(f"id must be snake_case ascii, got {value!r}")
    return value


def _check_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError("url must not contain credentials")
    return value


def _canonical_seed_url(value: str) -> str:
    parts = urlsplit(value.strip())
    path = (parts.path or "/").rstrip("/") or "/"
    port = parts.port
    host = (parts.hostname or "").lower()
    if port and not (
        (parts.scheme.lower() == "http" and port == 80)
        or (parts.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return f"{parts.scheme.lower()}://{host}{path}?{parts.query}".rstrip("?")


def _normalise_geo_code(value: str) -> str:
    code = value.strip().upper().replace(" ", "_")
    if not _GEO_CODE_RE.fullmatch(code):
        raise ValueError(f"invalid geography code: {value!r}")
    return code


def _normalise_dimension(value: str) -> str:
    dimension = value.strip().lower().replace("-", "_").replace(" ", "_")
    dimension = {"geo": "region", "geography": "region", "industry_id": "industry"}.get(
        dimension, dimension
    )
    if len(dimension) > 100 or not _DIMENSION_RE.fullmatch(dimension):
        raise ValueError(f"invalid target dimension: {value!r}")
    return dimension


def _normalise_generic_key(value: str) -> str:
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if len(key) > 300 or not key or not re.fullmatch(r"[a-z0-9][a-z0-9_.:/]*", key):
        raise ValueError(f"invalid target key: {value!r}")
    return key


def _check_collection_scope(value: dict | None) -> dict | None:
    if value is None:
        return None
    forbidden = {"api_key", "password", "token", "secret", "authorization"}
    exposed = forbidden & {str(key).strip().lower() for key in value}
    if exposed:
        raise ValueError(
            "collection_scope must use credential references, not secret fields: "
            + ", ".join(sorted(exposed))
        )
    endpoint = value.get("endpoint_url")
    if endpoint is not None:
        _check_url(str(endpoint))
    return value


class Manifest(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^pack_manifest/v1$")
    id: str
    name: str
    version: str
    description: str | None = None
    spec_compat: str = "phase1/v1"

    _id_ok = field_validator("id")(_check_id)

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not _SEMVER_RE.match(value):
            raise ValueError(f"version must be semver (x.y.z), got {value!r}")
        return value


class Term(StrictModel):
    term: str
    synonyms: list[str] = []
    notes: str | None = None


class TerminologyFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^terminology/v1$")
    terms: list[Term] = []
    exclusions: list[str] = []  # phrases that indicate a false match (spec §8.2)


class Archetype(StrictModel):
    id: str
    name: str
    description: str | None = None

    _id_ok = field_validator("id")(_check_id)


class ArchetypesFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^company_archetypes/v1$")
    archetypes: list[Archetype]

    @field_validator("archetypes")
    @classmethod
    def _unique_ids(cls, value: list[Archetype]) -> list[Archetype]:
        seen: set[str] = set()
        for archetype in value:
            if archetype.id in seen:
                raise ValueError(f"duplicate archetype id: {archetype.id}")
            seen.add(archetype.id)
        return value


class Service(StrictModel):
    id: str
    name: str
    description: str | None = None

    _id_ok = field_validator("id")(_check_id)


class ServiceCategory(StrictModel):
    id: str
    name: str
    services: list[Service]

    _id_ok = field_validator("id")(_check_id)


class ServiceTaxonomyFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^service_taxonomy/v1$")
    categories: list[ServiceCategory]


class IdName(StrictModel):
    id: str
    name: str
    description: str | None = None
    # Alternate spellings/brands used in the wild — consumed by deterministic page
    # extraction so vocabulary matching stays pack-configurable, never hard-coded.
    synonyms: list[str] = Field(default_factory=list)

    _id_ok = field_validator("id")(_check_id)


class MarketSegmentsFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^market_segments/v1$")
    segments: list[IdName]


class ProjectTypesFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^project_types/v1$")
    project_types: list[IdName]


class EventTypesFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^event_types/v1$")
    event_types: list[IdName]


class SystemsFile(StrictModel):
    """Products / scaffold systems / technologies (spec §8.4, §13.8)."""

    schema_id: str = Field(alias="schema", pattern=r"^products_systems/v1$")
    systems: list[IdName]


class SeedSource(StrictModel):
    """Legacy ``seed_sources/v1`` entry.

    V1 deliberately remains wire-compatible.  It is adapted by the source registry to
    one coherent coverage tuple for the declaring pack and jurisdiction.
    """

    name: str = Field(max_length=300)
    category: str = Field(max_length=50)
    url: str = Field(max_length=1000)
    access: AccessMethod
    jurisdiction: str
    authority_tier: int = Field(ge=1, le=7)
    terms_status: TermsStatusValue = "unreviewed"
    notes: str | None = None

    _url_ok = field_validator("url")(_check_url)

    @field_validator("name")
    @classmethod
    def _name_ok(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("category")
    @classmethod
    def _category_ok(cls, value: str) -> str:
        return _normalise_dimension(value)

    @field_validator("jurisdiction")
    @classmethod
    def _jurisdiction_ok(cls, value: str) -> str:
        parts = [part for chunk in value.split("/") for part in chunk.split(",")]
        if not any(part.strip() for part in parts):
            raise ValueError("jurisdiction must not be empty")
        for part in parts:
            if part.strip():
                _normalise_geo_code(part)
        return value.strip()


class SeedCoverageTarget(StrictModel):
    """One normalised target within a coherent coverage tuple."""

    dimension: str
    target_key: str = Field(
        max_length=300,
        validation_alias=AliasChoices("target_key", "key", "value", "id"),
    )
    target_label: str | None = Field(
        default=None,
        max_length=500,
        validation_alias=AliasChoices("target_label", "label"),
    )
    polarity: TargetPolarity = "include"
    match_mode: TargetMatchMode | None = None

    @model_validator(mode="before")
    @classmethod
    def _default_match_mode(cls, data):
        if isinstance(data, dict) and not data.get("match_mode"):
            data = dict(data)
            dimension = _normalise_dimension(str(data.get("dimension", "")))
            data["match_mode"] = "hierarchical" if dimension == "region" else "exact"
        return data

    @field_validator("dimension")
    @classmethod
    def _dimension_ok(cls, value: str) -> str:
        return _normalise_dimension(value)

    @field_validator("target_key")
    @classmethod
    def _target_key_ok(cls, value: str, info) -> str:
        dimension = info.data.get("dimension")
        if dimension == "region":
            return _normalise_geo_code(value)
        return _normalise_generic_key(value)

    @field_validator("target_label")
    @classmethod
    def _target_label_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class SeedCoverage(StrictModel):
    """A conjunction of dimensions; multiple coverages on a seed are alternatives."""

    key: str = Field(
        max_length=100,
        validation_alias=AliasChoices("key", "coverage_key", "id"),
    )
    name: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=20_000)
    lifecycle_status: Literal["active", "disabled"] = "active"
    priority: int = Field(default=50, ge=0, le=100)
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    authority_tier_override: int | None = Field(default=None, ge=1, le=7)
    collection_scope: dict | None = None
    parser_profile: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    industry_ids: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list)
    targets: list[SeedCoverageTarget] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalise_target_shorthands(cls, data):
        """Accept list or mapping shorthands for generic include/exclude targets."""
        if not isinstance(data, dict):
            return data
        result = dict(data)
        targets = list(result.get("targets") or [])
        for field_name, polarity in (
            ("include_targets", "include"),
            ("exclude_targets", "exclude"),
        ):
            raw = result.pop(field_name, None)
            if isinstance(raw, dict):
                for dimension, values in raw.items():
                    values = values if isinstance(values, list) else [values]
                    for value in values:
                        if isinstance(value, dict):
                            item = {"dimension": dimension, "polarity": polarity, **value}
                        else:
                            item = {
                                "dimension": dimension,
                                "target_key": value,
                                "polarity": polarity,
                            }
                        targets.append(item)
            elif isinstance(raw, list):
                for value in raw:
                    if not isinstance(value, dict):
                        raise ValueError(f"{field_name} list entries must be mappings")
                    targets.append({"polarity": polarity, **value})
            elif raw is not None:
                raise ValueError(f"{field_name} must be a mapping or list")
        result["targets"] = targets
        return result

    @field_validator("key")
    @classmethod
    def _key_ok(cls, value: str) -> str:
        return _check_id(value)

    @field_validator("industry_ids")
    @classmethod
    def _industry_ids_ok(cls, values: list[str]) -> list[str]:
        return sorted({_check_id(value.strip().lower()) for value in values})

    @field_validator("region_codes")
    @classmethod
    def _region_codes_ok(cls, values: list[str]) -> list[str]:
        return sorted({_normalise_geo_code(value) for value in values})

    @field_validator("parser_profile")
    @classmethod
    def _parser_profile_ok(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_generic_key(value)

    _collection_scope_ok = field_validator("collection_scope")(_check_collection_scope)

    @model_validator(mode="after")
    def _targets_consistent(self):
        for label, value in (("valid_from", self.valid_from), ("valid_to", self.valid_to)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{label} must include a timezone")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        seen: dict[tuple[str, str], str] = {}
        deduped: list[SeedCoverageTarget] = []
        for target in sorted(
            self.targets,
            key=lambda item: (
                item.dimension,
                item.target_key,
                item.polarity,
                item.match_mode or "",
            ),
        ):
            identity = (target.dimension, target.target_key)
            old_polarity = seen.get(identity)
            if old_polarity is not None and old_polarity != target.polarity:
                raise ValueError(
                    f"target {target.dimension}:{target.target_key} cannot be both "
                    "included and excluded"
                )
            if old_polarity is None:
                seen[identity] = target.polarity
                deduped.append(target)
        self.targets = deduped
        return self


class SeedSourceV2(StrictModel):
    """Stable, shareable source identity plus one or more applicability tuples."""

    key: str = Field(max_length=100, validation_alias=AliasChoices("key", "seed_key", "id"))
    source_key: str | None = Field(default=None, max_length=200)
    name: str = Field(max_length=300)
    category: str = Field(max_length=50)
    url: str = Field(max_length=1000)
    access: AccessMethod
    authority_tier: int = Field(default=5, ge=1, le=7)
    lifecycle_status: SeedSourceStatus = "candidate"
    terms_status: TermsStatusValue = "unreviewed"
    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    expected_update_frequency: str | None = Field(default=None, max_length=100)
    authentication_type: str | None = Field(default=None, max_length=50)
    rate_limit_policy: dict | None = None
    parser_profile: str | None = None
    collection_scope: dict | None = None
    notes: str | None = None
    coverages: list[SeedCoverage] = Field(min_length=1)

    _key_ok = field_validator("key")(_check_id)
    _url_ok = field_validator("url")(_check_url)
    _collection_scope_ok = field_validator("collection_scope")(_check_collection_scope)

    @field_validator("source_key")
    @classmethod
    def _source_key_ok(cls, value: str | None) -> str | None:
        return _check_id(value) if value is not None else None

    @field_validator("name")
    @classmethod
    def _name_ok(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("category")
    @classmethod
    def _category_ok(cls, value: str) -> str:
        return _normalise_dimension(value)

    @field_validator("parser_profile")
    @classmethod
    def _parser_profile_ok(cls, value: str | None) -> str | None:
        return _normalise_generic_key(value) if value is not None else None

    @field_validator("authentication_type")
    @classmethod
    def _authentication_type_ok(cls, value: str | None) -> str | None:
        return _normalise_generic_key(value) if value is not None else None

    @field_validator("expected_update_frequency")
    @classmethod
    def _frequency_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("expected_update_frequency must not be blank")
        return value

    @model_validator(mode="after")
    def _coverage_keys_unique(self):
        keys = [coverage.key for coverage in self.coverages]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate coverage key in source {self.key!r}")
        return self


class DiscoveryConfig(StrictModel):
    ai_expansion_enabled: bool = True
    proposals_require_approval: bool = True
    proposal_default_tier: int = Field(default=6, ge=1, le=7)
    expansion_targets: list[str] = []
    discovery_query_seeds: list[str] = []


class SeedSourcesFile(StrictModel):
    schema_id: str = Field(alias="schema", pattern=r"^seed_sources/v[12]$")
    pack: str
    sources: list[SeedSource | SeedSourceV2]
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)

    @model_validator(mode="after")
    def _version_and_identity_consistent(self):
        expected_type = SeedSourceV2 if self.schema_id.endswith("/v2") else SeedSource
        if any(not isinstance(source, expected_type) for source in self.sources):
            raise ValueError(
                f"{self.schema_id} contains an entry for the other seed-source schema version"
            )
        names = [source.name.strip().casefold() for source in self.sources]
        duplicate_names = sorted(name for name in set(names) if names.count(name) > 1)
        if duplicate_names:
            raise ValueError(f"duplicate source names: {', '.join(duplicate_names)}")
        urls = [_canonical_seed_url(source.url) for source in self.sources]
        duplicate_urls = sorted(url for url in set(urls) if urls.count(url) > 1)
        if duplicate_urls:
            raise ValueError(f"duplicate source URLs: {', '.join(duplicate_urls)}")
        if expected_type is SeedSourceV2:
            keys = [source.key for source in self.sources if isinstance(source, SeedSourceV2)]
            duplicate_keys = sorted(key for key in set(keys) if keys.count(key) > 1)
            if duplicate_keys:
                raise ValueError(f"duplicate seed keys: {', '.join(duplicate_keys)}")
            source_keys = [
                source.source_key
                for source in self.sources
                if isinstance(source, SeedSourceV2) and source.source_key
            ]
            duplicate_source_keys = sorted(
                key for key in set(source_keys) if source_keys.count(key) > 1
            )
            if duplicate_source_keys:
                raise ValueError(
                    f"duplicate canonical source keys: {', '.join(duplicate_source_keys)}"
                )
            too_long = [
                f"{source.key}:{coverage.key}"
                for source in self.sources
                if isinstance(source, SeedSourceV2)
                for coverage in source.coverages
                if len(f"pack:{self.pack}:{source.key}:{coverage.key}") > 200
            ]
            if too_long:
                raise ValueError("coverage identity exceeds 200 characters: " + ", ".join(too_long))
            long_names = [
                f"{source.key}:{coverage.key}"
                for source in self.sources
                if isinstance(source, SeedSourceV2)
                for coverage in source.coverages
                if len(coverage.name or f"{source.name} — {coverage.key}") > 300
            ]
            if long_names:
                raise ValueError(
                    "derived coverage name exceeds 300 characters: " + ", ".join(long_names)
                )
        else:
            derived_keys = [
                (re.sub(r"[^a-z0-9]+", "_", source.name.casefold()).strip("_") or "source")[:80]
                for source in self.sources
            ]
            duplicates = sorted(key for key in set(derived_keys) if derived_keys.count(key) > 1)
            if duplicates:
                raise ValueError(
                    "source names collide after stable-key normalisation: " + ", ".join(duplicates)
                )
        return self


# filename (relative to pack root) -> schema model.
# manifest.yaml is mandatory; the rest are optional but validated when present.
KNOWN_FILES: dict[str, type[StrictModel]] = {
    "manifest.yaml": Manifest,
    "terminology.yaml": TerminologyFile,
    "company_archetypes.yaml": ArchetypesFile,
    "service_taxonomy.yaml": ServiceTaxonomyFile,
    "market_segments.yaml": MarketSegmentsFile,
    "project_types.yaml": ProjectTypesFile,
    "event_types.yaml": EventTypesFile,
    "products_systems.yaml": SystemsFile,
    "sources/seed_sources.yaml": SeedSourcesFile,
}

REQUIRED_FILES = ("manifest.yaml",)
