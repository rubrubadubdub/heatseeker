"""Structured JSON logging to stderr + rotating file under the data path (spec §38)."""

import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from heatseeker_common.settings import Settings
from heatseeker_common.timeutil import utc_now

# Attributes present on every LogRecord — anything else was passed via extra={...}.
_STANDARD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": utc_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    settings: Settings,
    component: str,
    console_json: bool = True,
    console_level: str | None = None,
) -> None:
    """Idempotent per-process setup: logs to stderr and to data/logs/<component>.log.

    The file always gets full JSON at settings.log_level. The console defaults to the
    same, but user-facing entry points (heatseeker run) pass console_json=False and
    console_level="WARNING" so the window stays quiet and readable.
    """
    root = logging.getLogger()
    if getattr(root, "_heatseeker_configured", False):
        return
    root.setLevel(settings.log_level.upper())

    formatter = JsonFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    if console_json:
        stderr_handler.setFormatter(formatter)
    else:
        stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    if console_level:
        stderr_handler.setLevel(console_level.upper())
    root.addHandler(stderr_handler)

    settings.ensure_data_dirs()
    file_handler = RotatingFileHandler(
        settings.logs_dir / f"{component}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    root._heatseeker_configured = True  # type: ignore[attr-defined]
