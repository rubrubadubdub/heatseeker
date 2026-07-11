"""Shared fixtures: isolated Settings + migrated SQLite DB per test (tmp_path)."""

import pytest
from heatseeker_common.db import create_db_engine
from heatseeker_common.migrate import upgrade_to_head
from heatseeker_common.settings import Settings
from heatseeker_core_domain.geography import reset_macro_regions
from sqlalchemy.engine import Engine


@pytest.fixture(autouse=True)
def _isolated_geo_registry():
    # The named-region registry is process-global and mutated by app startup and
    # region edits (ADR-0012); restore builtins after every test.
    yield
    reset_macro_regions()


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
