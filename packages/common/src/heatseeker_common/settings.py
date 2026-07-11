"""Application settings. All values overridable via HEATSEEKER_* env vars or .env."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="HEATSEEKER_",
        extra="ignore",
    )

    data_dir: Path = Path("data")
    # When None, derived from data_dir (SQLite per ADR-0007).
    database_url: str | None = None
    api_host: str = "127.0.0.1"  # localhost-only default is a spec requirement (§32.1)
    api_port: int = 8100
    log_level: str = "INFO"
    worker_poll_interval: float = 0.5
    worker_heartbeat_interval: float = 10.0
    # RUNNING jobs whose heartbeat is older than this get requeued/failed by the reaper.
    stale_job_seconds: float = 120.0
    # Collection identity + limits (spec §11.2, §11.5). Set a real contact before
    # heavy collection: e.g. "HeatseekerResearch/0.1 (+mailto:you@example.com)"
    crawler_user_agent: str = "HeatseekerResearch/0.1 (local industry research tool)"
    fetch_timeout_seconds: float = 20.0
    fetch_max_bytes: int = 10 * 1024 * 1024
    # Politeness + storage economy (spec §11.5; ADR-0011)
    politeness_delay_seconds: float = 2.0
    politeness_jitter_seconds: float = 1.5
    collect_due_batch_limit: int = 10
    robots_recheck_days: float = 7.0
    store_compression: bool = True
    # Robots policy (ADR-0013, owner directive). "enforce" honours robots.txt Disallow;
    # "ignore" collects regardless while KEEPING the identified user-agent, politeness
    # delays, and conditional GETs — the sweet spot for a private, low-volume research
    # tool that wants the data without burning exit IPs. Robots status is still fetched
    # and recorded in evidence provenance either way. A per-source override wins over this.
    robots_policy: str = "ignore"
    # Optional outbound proxy for every fetch (basic VPN/region seam, ADR-0013), e.g.
    # "http://host:8080" or "socks5://user:pass@host:1080" (SOCKS needs httpx[socks]).
    # Ignored when a test transport is injected. Per-region auto-switching is a tracked
    # TODO (roadmap) — today one proxy applies to all collection + crawl traffic.
    fetch_proxy_url: str | None = None
    # Autopilot: the worker self-drives seed sync, policy checks, activation,
    # collection, and maintenance (ADR-0011). Disable to go fully manual.
    autopilot_enabled: bool = True
    autopilot_interval_seconds: float = 300.0
    autopilot_policy_batch: int = 5
    autopilot_maintenance_hours: float = 24.0
    # Crawl budgets (spec §11.6, §24.4) — deterministic bounds on every crawl run
    crawl_max_pages: int = 30
    crawl_max_depth: int = 2
    crawl_max_new_domains: int = 10
    crawl_stale_streak_stop: int = 8
    # Frontier rows older than this are re-queued on the next crawl so change
    # detection keeps working over time (re-fetches dedupe by content hash).
    crawl_recrawl_hours: float = 24.0

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return upper

    @field_validator("robots_policy")
    @classmethod
    def _valid_robots_policy(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in {"enforce", "ignore"}:
            raise ValueError(f"robots_policy must be 'enforce' or 'ignore', got {value!r}")
        return lowered

    @field_validator("fetch_proxy_url")
    @classmethod
    def _valid_fetch_proxy_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
            raise ValueError("fetch_proxy_url must be an absolute HTTP(S) or SOCKS5 URL")
        return value

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.resolved_data_dir / "heatseeker.db"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{self.database_path.as_posix()}"

    @property
    def raw_dir(self) -> Path:
        return self.resolved_data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.resolved_data_dir / "processed"

    @property
    def exports_dir(self) -> Path:
        return self.resolved_data_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.resolved_data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.resolved_data_dir / "logs"

    def data_paths(self) -> dict[str, Path]:
        return {
            "data": self.resolved_data_dir,
            "raw": self.raw_dir,
            "processed": self.processed_dir,
            "exports": self.exports_dir,
            "backups": self.backups_dir,
            "logs": self.logs_dir,
        }

    def ensure_data_dirs(self) -> dict[str, Path]:
        paths = self.data_paths()
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
