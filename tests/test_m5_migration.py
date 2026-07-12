"""Forward/backward compatibility for the M5 hardening migration."""

from alembic import command
from heatseeker_common.db import create_db_engine
from heatseeker_common.migrate import build_alembic_config
from heatseeker_common.settings import Settings
from sqlalchemy import inspect, text


def test_populated_0015_database_roundtrips_m5_hardening(tmp_path):
    settings = Settings(data_dir=tmp_path / "legacy-m5", _env_file=None)
    settings.ensure_data_dirs()
    config = build_alembic_config(settings)
    command.upgrade(config, "0015")

    engine = create_db_engine(settings)
    now = "2026-07-12 00:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO bulk_import_run (
                    id, dataset_name, publisher, dataset_version, coverage_date,
                    licence_note, checksum, mapping, source_definition_id,
                    source_document_id, status, row_count, imported_count,
                    matched_existing_count, skipped_out_of_scope_count, rejected_count,
                    rejected_samples, transformation_version, error, actor, created_at,
                    finished_at
                ) VALUES (
                    'legacy-run', 'Legacy', NULL, NULL, NULL, NULL, :checksum, '{}',
                    NULL, NULL, 'succeeded', 1, 1, 0, 0, 0, '[]', 'import/0.1',
                    NULL, 'user', :now, :now
                )
                """
            ),
            {"checksum": "a" * 64, "now": now},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_db_engine(settings)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT authority_tier, scope_snapshot, pack_snapshot FROM bulk_import_run "
                "WHERE id = 'legacy-run'"
            )
        ).one()
        assert tuple(row) == (5, None, None)
        observation_columns = {
            column["name"] for column in inspect(connection).get_columns("observation")
        }
        assert {"human_verified", "verified_by", "verified_at"} <= observation_columns
    engine.dispose()

    command.downgrade(config, "0015")
    engine = create_db_engine(settings)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT dataset_name FROM bulk_import_run WHERE id = 'legacy-run'")
        ).scalar_one() == "Legacy"
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("bulk_import_run")
        }
        assert "authority_tier" not in columns
    engine.dispose()
