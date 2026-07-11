"""Infrastructure tables only (M0). Domain tables arrive at M4 — keep them out of here."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from heatseeker_common.db import Base, UTCDateTime
from heatseeker_common.timeutil import utc_now


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PriorityClass(enum.IntEnum):
    """Spec §30.3. Lower value = claimed first."""

    INTERACTIVE = 10
    HIGH_VALUE_REFRESH = 20
    EVENT_TRIGGERED = 30
    SCHEDULED_PRIORITY = 40
    BACKGROUND_ENRICHMENT = 50
    LOW_PRIORITY_DISCOVERY = 60
    MAINTENANCE = 70


def _new_id() -> str:
    return str(uuid.uuid4())


class Job(Base):
    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    job_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, default=int(PriorityClass.BACKGROUND_ENRICHMENT))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_job_claim", "status", "run_at", "priority"),)


class AuditLog(Base):
    """Spec §31.3 — automated and user actions, append-only."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100), index=True)
    subject_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class WorkerRegistration(Base):
    __tablename__ = "worker_registration"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(200))
    pid: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class AppMeta(Base):
    """Instance-level key/value metadata (instance id, versions)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
