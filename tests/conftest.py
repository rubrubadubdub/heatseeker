"""Shared fixtures: isolated Settings + migrated SQLite DB per test (tmp_path)."""

import pytest
from heatseeker_common.db import create_db_engine
from heatseeker_common.migrate import upgrade_to_head
from heatseeker_common.settings import Settings
from sqlalchemy.engine import Engine


@pytest.fixture()
def settings(tmp_path) -> Settings:
    # _env_file=None: ignore any repo-level .env so tests are hermetic.
    return Settings(data_dir=tmp_path / "data", _env_file=None)


@pytest.fixture()
def engine(settings) -> Engine:
    upgrade_to_head(settings)
    engine = create_db_engine(settings)
    yield engine
    engine.dispose()
