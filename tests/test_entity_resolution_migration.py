"""Forward/backward compatibility for the M4 refinement migration."""

from alembic import command
from heatseeker_common.db import create_db_engine
from heatseeker_common.migrate import build_alembic_config
from heatseeker_common.settings import Settings
from sqlalchemy import inspect, text


def test_populated_0013_database_roundtrips_m4_refinement(tmp_path):
    settings = Settings(data_dir=tmp_path / "legacy-data", _env_file=None)
    settings.ensure_data_dirs()
    config = build_alembic_config(settings)
    command.upgrade(config, "0013")

    engine = create_db_engine(settings)
    now = "2026-07-12 00:00:00"
    organisation_sql = text(
        """
        INSERT INTO organisation (
            id, canonical_name, legal_name, trading_names, organisation_type, status,
            country_of_registration, parent_organisation_id, ultimate_parent_id,
            primary_location_id, description, provenance, first_observed_at,
            last_observed_at, profile_completeness, entity_confidence, merged_into_id
        ) VALUES (
            :id, :name, NULL, '[]', 'company', 'active', NULL, NULL, NULL, NULL,
            NULL, 'manual', :now, :now, 0.0, 0.5, NULL
        )
        """
    )
    with engine.begin() as connection:
        connection.execute(organisation_sql, {"id": "org-a", "name": "Acme", "now": now})
        connection.execute(organisation_sql, {"id": "org-b", "name": "ACME", "now": now})
        connection.execute(
            text(
                """
                INSERT INTO entity_match_candidate (
                    id, organisation_a_id, organisation_b_id, match_state, score,
                    signals, conflict_count, resolution, resolved_by, resolved_at,
                    notes, created_at, updated_at
                ) VALUES (
                    'candidate-1', 'org-a', 'org-b', 'possible_review', 0.7,
                    '[]', 0, NULL, NULL, NULL, NULL, :now, :now
                )
                """
            ),
            {"now": now},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_db_engine(settings)
    with engine.connect() as connection:
        candidate = connection.execute(
            text(
                """
                SELECT priority_score, commercial_importance, downstream_impact,
                       ease_of_resolution
                FROM entity_match_candidate WHERE id = 'candidate-1'
                """
            )
        ).one()
        assert tuple(candidate) == (0.7, 0.0, 0.0, 0.0)
        merge_columns = {
            column["name"] for column in inspect(connection).get_columns("entity_merge")
        }
        assert {"candidate_prior_match_state", "reversed_by"} <= merge_columns
    engine.dispose()

    command.downgrade(config, "0013")
    engine = create_db_engine(settings)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT score FROM entity_match_candidate WHERE id = 'candidate-1'")
        ).scalar_one() == 0.7
        candidate_columns = {
            column["name"]
            for column in inspect(connection).get_columns("entity_match_candidate")
        }
        assert "priority_score" not in candidate_columns
    engine.dispose()
