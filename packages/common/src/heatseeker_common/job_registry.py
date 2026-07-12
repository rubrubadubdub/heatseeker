"""Job handler registry. Handlers are plain functions registered by job_type string."""

from collections.abc import Callable
from dataclasses import dataclass
from logging import Logger

from sqlalchemy.engine import Engine

from heatseeker_common.settings import Settings


class PermanentJobError(RuntimeError):
    """A deterministic payload/entity error that must not be retried."""


class JobCancelled(RuntimeError):
    """A running handler cooperatively stopped after cancellation was requested."""


@dataclass
class JobContext:
    job_id: str
    job_type: str
    payload: dict
    attempt: int
    engine: Engine
    logger: Logger
    settings: Settings


JobHandler = Callable[[JobContext], dict | None]

_REGISTRY: dict[str, JobHandler] = {}


def job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def register(fn: JobHandler) -> JobHandler:
        if job_type in _REGISTRY:
            raise ValueError(f"duplicate job handler: {job_type}")
        _REGISTRY[job_type] = fn
        return fn

    return register


def get_handler(job_type: str) -> JobHandler | None:
    return _REGISTRY.get(job_type)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
