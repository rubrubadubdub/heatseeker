from pathlib import Path

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
