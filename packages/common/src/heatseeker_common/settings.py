"""Application settings. All values overridable via HEATSEEKER_* env vars or .env."""

from functools import lru_cache
from pathlib import Path

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
    fetch_document_max_bytes: int = 50 * 1024 * 1024
    fetch_image_max_bytes: int = 20 * 1024 * 1024
    evidence_upload_max_bytes: int = 50 * 1024 * 1024
    # Politeness + storage economy (spec §11.5; ADR-0011)
    politeness_delay_seconds: float = 2.0
    politeness_jitter_seconds: float = 1.5
    collect_due_batch_limit: int = 10
    robots_recheck_days: float = 7.0
    store_compression: bool = True
    # Robots enforcement (ADR-0013). "enforce" (default) honours robots.txt Disallow
    # rules; "ignore" treats them as advisory for endpoints you are authorised to
    # collect. A per-source override takes precedence. Robots status is always fetched
    # and recorded in evidence provenance regardless of this setting.
    robots_policy: str = "enforce"
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
    crawl_max_documents: int = 20
    crawl_max_images: int = 30
    crawl_max_images_per_page: int = 12
    # Frontier rows older than this are re-queued on the next crawl so change
    # detection keeps working over time (re-fetches dedupe by content hash).
    crawl_recrawl_hours: float = 24.0
    # Derived evidence processing. Raw input is always preserved first; these bounds
    # limit work performed on untrusted PDFs, OOXML containers, and images.
    document_max_pages: int = 250
    document_max_extracted_chars: int = 2_000_000
    document_zip_max_entries: int = 2_000
    document_zip_max_uncompressed_bytes: int = 100 * 1024 * 1024
    document_zip_max_ratio: float = 100.0
    image_max_pixels: int = 40_000_000
    image_max_frames: int = 10
    # OCR and semantic vision require explicit providers. They remain off when no real
    # provider is configured; metadata-only processing must continue to work.
    evidence_ocr_enabled: bool = False
    evidence_vision_enabled: bool = False

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

    @field_validator(
        "crawl_max_documents",
        "crawl_max_images",
        "crawl_max_images_per_page",
        "document_max_pages",
        "document_max_extracted_chars",
        "document_zip_max_entries",
        "document_zip_max_uncompressed_bytes",
        "image_max_pixels",
        "image_max_frames",
        "fetch_max_bytes",
        "fetch_document_max_bytes",
        "fetch_image_max_bytes",
        "evidence_upload_max_bytes",
    )
    @classmethod
    def _positive_evidence_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("evidence collection and processing limits must be positive")
        return value

    @field_validator("document_zip_max_ratio")
    @classmethod
    def _positive_zip_ratio(cls, value: float) -> float:
        if value <= 1:
            raise ValueError("document_zip_max_ratio must be greater than 1")
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
