from pathlib import Path

import pytest
from heatseeker_common.settings import Settings


def test_database_url_derived_from_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    url = settings.resolved_database_url
    assert url.startswith("sqlite+pysqlite:///")
    assert url.endswith("heatseeker.db")


def test_explicit_database_url_wins(tmp_path):
    settings = Settings(
        data_dir=tmp_path, database_url="sqlite+pysqlite:///elsewhere.db", _env_file=None
    )
    assert settings.resolved_database_url == "sqlite+pysqlite:///elsewhere.db"


def test_ensure_data_dirs_creates_all(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    paths = settings.ensure_data_dirs()
    assert set(paths) == {"data", "raw", "processed", "exports", "backups", "logs"}
    for path in paths.values():
        assert Path(path).is_dir()


def test_api_binds_localhost_by_default(tmp_path):
    assert Settings(data_dir=tmp_path, _env_file=None).api_host == "127.0.0.1"


def test_robots_defaults_to_advisory_and_validates(tmp_path):
    assert Settings(data_dir=tmp_path, _env_file=None).robots_policy == "ignore"
    assert Settings(data_dir=tmp_path, robots_policy=" ENFORCE ", _env_file=None).robots_policy == (
        "enforce"
    )
    with pytest.raises(ValueError, match="robots_policy"):
        Settings(data_dir=tmp_path, robots_policy="sometimes", _env_file=None)


def test_fetch_proxy_url_is_normalised_and_validated(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        fetch_proxy_url="  socks5://localhost:1080  ",
        _env_file=None,
    )
    assert settings.fetch_proxy_url == "socks5://localhost:1080"
    assert Settings(data_dir=tmp_path, fetch_proxy_url="  ", _env_file=None).fetch_proxy_url is None
    with pytest.raises(ValueError, match="fetch_proxy_url"):
        Settings(data_dir=tmp_path, fetch_proxy_url="localhost:8080", _env_file=None)
