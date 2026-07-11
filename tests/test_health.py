from heatseeker_common.db import create_db_engine
from heatseeker_common.health import check_health
from heatseeker_common.settings import Settings


def test_health_ok_on_migrated_db(engine, settings):
    report = check_health(engine, settings)
    assert report["status"] == "ok"
    assert report["checks"]["database"]["status"] == "ok"
    assert report["checks"]["migrations"]["status"] == "ok"
    assert report["checks"]["data_paths"]["status"] == "ok"
    assert report["checks"]["disk"]["status"] in ("ok", "warn")


def test_health_degraded_without_migrations(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    engine = create_db_engine(settings)  # empty DB, no alembic_version table
    try:
        report = check_health(engine, settings)
    finally:
        engine.dispose()
    assert report["status"] == "degraded"
    assert report["checks"]["migrations"]["status"] == "fail"
