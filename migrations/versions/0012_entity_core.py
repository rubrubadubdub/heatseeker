"""Entity core & resolution (M4): organisations, units, locations, identifiers,
domains, contact points, match candidates, reversible merges.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "location",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("address_lines", sa.JSON(), nullable=False),
        sa.Column("locality", sa.String(200), nullable=True),
        sa.Column("region", sa.String(200), nullable=True),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("location_type", sa.String(30), nullable=False),
        sa.Column("geocode_confidence", sa.Float(), nullable=True),
    )
    op.create_index("ix_location_locality", "location", ["locality"])

    op.create_table(
        "organisation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("canonical_name", sa.String(500), nullable=False),
        sa.Column("legal_name", sa.String(500), nullable=True),
        sa.Column("trading_names", sa.JSON(), nullable=False),
        sa.Column("organisation_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("country_of_registration", sa.String(100), nullable=True),
        sa.Column("parent_organisation_id", sa.String(36), nullable=True),
        sa.Column("ultimate_parent_id", sa.String(36), nullable=True),
        sa.Column("primary_location_id", sa.String(36), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provenance", sa.String(20), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.Column("profile_completeness", sa.Float(), nullable=False),
        sa.Column("entity_confidence", sa.Float(), nullable=False),
        sa.Column("merged_into_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(
            ["parent_organisation_id"],
            ["organisation.id"],
            name="fk_organisation_parent_organisation_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["ultimate_parent_id"],
            ["organisation.id"],
            name="fk_organisation_ultimate_parent_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["primary_location_id"],
            ["location.id"],
            name="fk_organisation_primary_location_id_location",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["organisation.id"],
            name="fk_organisation_merged_into_id_organisation",
        ),
        sa.CheckConstraint(
            "length(trim(canonical_name)) > 0",
            name="ck_organisation_canonical_name_nonempty",
        ),
        sa.CheckConstraint("id != merged_into_id", name="ck_organisation_no_self_merge"),
    )
    op.create_index("ix_organisation_canonical_name", "organisation", ["canonical_name"])
    op.create_index("ix_organisation_status", "organisation", ["status"])
    op.create_index("ix_organisation_merged_into_id", "organisation", ["merged_into_id"])

    op.create_table(
        "operational_unit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("unit_type", sa.String(30), nullable=False),
        sa.Column("name", sa.String(300), nullable=True),
        sa.Column("location_id", sa.String(36), nullable=True),
        sa.Column("service_area", sa.JSON(), nullable=True),
        sa.Column("active_status", sa.String(20), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_operational_unit_organisation_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["location.id"], name="fk_operational_unit_location_id_location"
        ),
    )
    op.create_index("ix_operational_unit_organisation_id", "operational_unit", ["organisation_id"])

    op.create_table(
        "organisation_identifier",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("scheme", sa.String(30), nullable=False),
        sa.Column("value", sa.String(100), nullable=False),
        sa.Column("value_normalised", sa.String(100), nullable=False),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_organisation_identifier_organisation_id_organisation",
        ),
        sa.UniqueConstraint(
            "organisation_id",
            "scheme",
            "value_normalised",
            name="uq_organisation_identifier_organisation_id",
        ),
        sa.CheckConstraint(
            "length(trim(value)) > 0", name="ck_organisation_identifier_value_nonempty"
        ),
    )
    op.create_index(
        "ix_organisation_identifier_organisation_id",
        "organisation_identifier",
        ["organisation_id"],
    )
    op.create_index(
        "ix_organisation_identifier_lookup",
        "organisation_identifier",
        ["scheme", "value_normalised"],
    )

    op.create_table(
        "organisation_domain",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("domain", sa.String(300), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_organisation_domain_organisation_id_organisation",
        ),
        sa.UniqueConstraint(
            "organisation_id", "domain", name="uq_organisation_domain_organisation_id"
        ),
        sa.CheckConstraint(
            "length(trim(domain)) > 0", name="ck_organisation_domain_domain_nonempty"
        ),
    )
    op.create_index(
        "ix_organisation_domain_organisation_id", "organisation_domain", ["organisation_id"]
    )
    op.create_index("ix_organisation_domain_domain", "organisation_domain", ["domain"])

    op.create_table(
        "contact_point",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_id", sa.String(36), nullable=False),
        sa.Column("operational_unit_id", sa.String(36), nullable=True),
        sa.Column("contact_type", sa.String(30), nullable=False),
        sa.Column("value", sa.String(500), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("public_business_contact", sa.Boolean(), nullable=False),
        sa.Column("role_based", sa.Boolean(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("deliverability_status", sa.String(30), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_evidence_ids", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_id"],
            ["organisation.id"],
            name="fk_contact_point_organisation_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["operational_unit_id"],
            ["operational_unit.id"],
            name="fk_contact_point_operational_unit_id_operational_unit",
        ),
        sa.CheckConstraint("length(trim(value)) > 0", name="ck_contact_point_value_nonempty"),
    )
    op.create_index("ix_contact_point_organisation_id", "contact_point", ["organisation_id"])

    op.create_table(
        "entity_match_candidate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organisation_a_id", sa.String(36), nullable=False),
        sa.Column("organisation_b_id", sa.String(36), nullable=False),
        sa.Column("match_state", sa.String(30), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(20), nullable=True),
        sa.Column("resolved_by", sa.String(100), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organisation_a_id"],
            ["organisation.id"],
            name="fk_entity_match_candidate_organisation_a_id_organisation",
        ),
        sa.ForeignKeyConstraint(
            ["organisation_b_id"],
            ["organisation.id"],
            name="fk_entity_match_candidate_organisation_b_id_organisation",
        ),
        sa.UniqueConstraint(
            "organisation_a_id",
            "organisation_b_id",
            name="uq_entity_match_candidate_organisation_a_id",
        ),
        sa.CheckConstraint(
            "organisation_a_id < organisation_b_id",
            name="ck_entity_match_candidate_pair_ordered",
        ),
    )
    op.create_index(
        "ix_entity_match_candidate_organisation_a_id",
        "entity_match_candidate",
        ["organisation_a_id"],
    )
    op.create_index(
        "ix_entity_match_candidate_organisation_b_id",
        "entity_match_candidate",
        ["organisation_b_id"],
    )
    op.create_index(
        "ix_entity_match_candidate_match_state", "entity_match_candidate", ["match_state"]
    )

    op.create_table(
        "entity_merge",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("survivor_id", sa.String(36), nullable=False),
        sa.Column("absorbed_id", sa.String(36), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("signals_snapshot", sa.JSON(), nullable=False),
        sa.Column("absorbed_prior_status", sa.String(20), nullable=False),
        sa.Column("performed_by", sa.String(100), nullable=False),
        sa.Column("performed_at", sa.DateTime(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["survivor_id"], ["organisation.id"], name="fk_entity_merge_survivor_id_organisation"
        ),
        sa.ForeignKeyConstraint(
            ["absorbed_id"], ["organisation.id"], name="fk_entity_merge_absorbed_id_organisation"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["entity_match_candidate.id"],
            name="fk_entity_merge_candidate_id_entity_match_candidate",
        ),
        sa.CheckConstraint(
            "survivor_id != absorbed_id", name="ck_entity_merge_distinct_parties"
        ),
    )
    op.create_index("ix_entity_merge_survivor_id", "entity_merge", ["survivor_id"])
    op.create_index("ix_entity_merge_absorbed_id", "entity_merge", ["absorbed_id"])


def downgrade() -> None:
    op.drop_table("entity_merge")
    op.drop_table("entity_match_candidate")
    op.drop_table("contact_point")
    op.drop_table("organisation_domain")
    op.drop_table("organisation_identifier")
    op.drop_table("operational_unit")
    op.drop_table("organisation")
    op.drop_table("location")
