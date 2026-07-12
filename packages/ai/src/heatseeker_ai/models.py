"""Persistent source-scout plans, runs, AI audit records, and proposals."""

import enum
import uuid
from datetime import datetime

from heatseeker_common.db import Base, UTCDateTime
from heatseeker_common.timeutil import utc_now
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _new_id() -> str:
    return str(uuid.uuid4())


class ScoutProvider(enum.StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    DISABLED = "disabled"


class ActivationMode(enum.StrEnum):
    PROPOSAL_ONLY = "proposal_only"
    AUTO_ACTIVATE = "auto_activate"


class ScoutRunStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProposalStatus(enum.StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    AUTO_ACTIVATED = "auto_activated"


class ResearchPlan(Base):
    __tablename__ = "research_plan"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("research_scope.id", ondelete="SET NULL"), nullable=True
    )
    search_config: Mapped[dict] = mapped_column(JSON, default=dict)
    instructions: Mapped[str] = mapped_column(Text, default="")
    budgets: Mapped[dict] = mapped_column(JSON, default=dict)
    activation_mode: Mapped[str] = mapped_column(String(30), default=ActivationMode.PROPOSAL_ONLY)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)

    runs: Mapped[list["ResearchRun"]] = relationship(back_populates="plan")

    __table_args__ = (
        CheckConstraint("provider IN ('codex','claude','disabled')", name="provider"),
        CheckConstraint(
            "activation_mode IN ('proposal_only','auto_activate')", name="activation_mode"
        ),
        CheckConstraint(
            "interval_minutes IS NULL OR interval_minutes >= 5", name="interval_minutes_minimum"
        ),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
    )


class ResearchRun(Base):
    __tablename__ = "research_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("research_plan.id", ondelete="RESTRICT"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("job.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ScoutRunStatus.QUEUED, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    plan_snapshot: Mapped[dict] = mapped_column(JSON)
    scope_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    counters: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    plan: Mapped[ResearchPlan] = relationship(back_populates="runs")
    invocations: Mapped[list["AIInvocation"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    proposals: Mapped[list["SourceProposal"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="status"
        ),
        CheckConstraint("trigger IN ('manual','schedule')", name="trigger"),
        Index("ix_research_run_plan_created", "plan_id", "created_at"),
    )


class AIInvocation(Base):
    __tablename__ = "ai_invocation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("research_run.id", ondelete="CASCADE"), index=True
    )
    task_name: Mapped[str] = mapped_column(String(100), default="source.propose_expansion")
    prompt_version: Mapped[str] = mapped_column(String(50))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    input_payload: Mapped[dict] = mapped_column(JSON)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(20), default="pending")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    run: Mapped[ResearchRun] = relationship(back_populates="invocations")

    __table_args__ = (
        CheckConstraint(
            "validation_status IN ('pending','valid','invalid','failed')",
            name="validation_status",
        ),
    )


class SourceProposal(Base):
    __tablename__ = "source_proposal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("research_run.id", ondelete="CASCADE"), index=True
    )
    source_definition_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_definition.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default=ProposalStatus.PROPOSED, index=True)
    url: Mapped[str] = mapped_column(String(2000))
    normalised_url: Mapped[str] = mapped_column(String(2000), index=True)
    name: Mapped[str] = mapped_column(String(300))
    source_category: Mapped[str] = mapped_column(String(50))
    access_method: Mapped[str] = mapped_column(String(20))
    suggested_authority_tier: Mapped[int] = mapped_column(Integer, default=6)
    reasoning: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    originating_query: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    supporting_urls: Mapped[list] = mapped_column(JSON, default=list)
    suggested_coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    run: Mapped[ResearchRun] = relationship(back_populates="proposals")

    __table_args__ = (
        UniqueConstraint("run_id", "normalised_url", name="uq_source_proposal_run_url"),
        CheckConstraint(
            "status IN ('proposed','accepted','rejected','duplicate','invalid','auto_activated')",
            name="status",
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        CheckConstraint(
            "suggested_authority_tier >= 1 AND suggested_authority_tier <= 7",
            name="authority_tier_range",
        ),
    )
