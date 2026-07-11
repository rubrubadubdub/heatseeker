"""Programmatic Alembic access. Migrations live at <repo_root>/migrations."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from heatseeker_common.settings import Settings


def find_migrations_dir(start: Path | None = None) -> Path:
    """Locate migrations/: env var override, then upward from cwd, then from this file
    (editable installs live inside the repo, so walking up from __file__ works in dev)."""
    override = os.environ.get("HEATSEEKER_MIGRATIONS_DIR")
    if override:
        path = Path(override)
        if (path / "env.py").exists():
            return path
        raise FileNotFoundError(f"HEATSEEKER_MIGRATIONS_DIR has no env.py: {path}")

    candidates = [start or Path.cwd(), Path(__file__).resolve()]
    for anchor in candidates:
        for parent in [anchor, *anchor.parents]:
            found = parent / "migrations"
            if (found / "env.py").exists():
                return found
    raise FileNotFoundError("could not locate migrations/ (set HEATSEEKER_MIGRATIONS_DIR)")


def build_alembic_config(settings: Settings, migrations_dir: Path | None = None) -> Config:
    migrations_dir = migrations_dir or find_migrations_dir()
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", settings.resolved_database_url)
    return cfg


def upgrade_to_head(settings: Settings, migrations_dir: Path | None = None) -> None:
    settings.ensure_data_dirs()
    command.upgrade(build_alembic_config(settings, migrations_dir), "head")


def script_head(migrations_dir: Path | None = None) -> str | None:
    migrations_dir = migrations_dir or find_migrations_dir()
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    return ScriptDirectory.from_config(cfg).get_current_head()
