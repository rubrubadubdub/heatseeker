"""Schema-constrained source-expansion task contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class CoverageSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")
    targets: list[str] = Field(default_factory=list, max_length=20)


class CandidateSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=300)
    url: HttpUrl
    source_category: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    access_method: Literal["api", "bulk", "rss", "sitemap", "html", "rendered", "manual"]
    reasoning: str = Field(min_length=1, max_length=10_000)
    confidence: float = Field(ge=0, le=1)
    authority_tier: int = Field(default=6, ge=1, le=7)
    originating_query: str | None = Field(default=None, max_length=1000)
    supporting_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    suggested_coverage: list[CoverageSuggestion] = Field(default_factory=list, max_length=10)


class SourceExpansionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=10_000)
    queries_used: list[str] = Field(default_factory=list, max_length=100)
    candidates: list[CandidateSource] = Field(default_factory=list, max_length=200)
    coverage_gaps: list[str] = Field(default_factory=list, max_length=100)
    explicit_unknowns: list[str] = Field(default_factory=list, max_length=100)


class SourceExpansionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: dict | None = None
    search_config: dict = Field(default_factory=dict)
    instructions: str = Field(default="", max_length=20_000)
    budgets: dict = Field(default_factory=dict)
    existing_domains: list[str] = Field(default_factory=list, max_length=10_000)


class EntityPageCandidate(BaseModel):
    """A public URL proposed for deterministic verification by HeatSeeker."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    page_type: Literal[
        "official_website",
        "official_contact",
        "official_services",
        "official_location",
        "official_registry",
        "business_directory",
        "public_profile",
        "other",
    ]
    reasoning: str = Field(min_length=1, max_length=5000)
    confidence: float = Field(ge=0, le=1)
    matching_identifiers: list[str] = Field(default_factory=list, max_length=20)
    supported_fields: list[str] = Field(default_factory=list, max_length=20)


class EntityWebResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=10_000)
    queries_used: list[str] = Field(default_factory=list, max_length=50)
    candidates: list[EntityPageCandidate] = Field(default_factory=list, max_length=50)
    explicit_unknowns: list[str] = Field(default_factory=list, max_length=50)
