"""AI source-scout plans, runs, invocations, and proposals.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_plan",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("scope_id", sa.String(36), nullable=True),
        sa.Column("search_config", sa.JSON(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("budgets", sa.JSON(), nullable=False),
        sa.Column("activation_mode", sa.String(30), nullable=False),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scope_id"], ["research_scope.id"], name="fk_research_plan_scope_id_research_scope"
        ),
        sa.CheckConstraint(
            "provider IN ('codex','claude','disabled')", name="ck_research_plan_provider"
        ),
        sa.CheckConstraint(
            "activation_mode IN ('proposal_only','auto_activate')",
            name="ck_research_plan_activation_mode",
        ),
        sa.CheckConstraint(
            "interval_minutes IS NULL OR interval_minutes >= 5",
            name="ck_research_plan_interval_minutes_minimum",
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_research_plan_name_nonempty"),
    )
    op.create_index("ix_research_plan_next_run_at", "research_plan", ["next_run_at"])

    op.create_table(
        "research_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=True, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False),
        sa.Column("scope_snapshot", sa.JSON(), nullable=True),
        sa.Column("counters", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["research_plan.id"], name="fk_research_run_plan_id_research_plan"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name="fk_research_run_job_id_job"),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_research_run_status",
        ),
        sa.CheckConstraint("trigger IN ('manual','schedule')", name="ck_research_run_trigger"),
    )
    op.create_index("ix_research_run_plan_id", "research_run", ["plan_id"])
    op.create_index("ix_research_run_status", "research_run", ["status"])
    op.create_index("ix_research_run_plan_created", "research_run", ["plan_id", "created_at"])

    op.create_table(
        "ai_invocation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("task_name", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("validated_output", sa.JSON(), nullable=True),
        sa.Column("validation_status", sa.String(20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name="fk_ai_invocation_run_id_research_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "validation_status IN ('pending','valid','invalid','failed')",
            name="ck_ai_invocation_validation_status",
        ),
    )
    op.create_index("ix_ai_invocation_run_id", "ai_invocation", ["run_id"])
    op.create_index("ix_ai_invocation_input_hash", "ai_invocation", ["input_hash"])

    op.create_table(
        "source_proposal",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("source_definition_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("normalised_url", sa.String(2000), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("source_category", sa.String(50), nullable=False),
        sa.Column("access_method", sa.String(20), nullable=False),
        sa.Column("suggested_authority_tier", sa.Integer(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("originating_query", sa.String(1000), nullable=True),
        sa.Column("supporting_urls", sa.JSON(), nullable=False),
        sa.Column("suggested_coverage", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_run.id"],
            name="fk_source_proposal_run_id_research_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_definition_id"],
            ["source_definition.id"],
            name="fk_source_proposal_source_definition_id_source_definition",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("run_id", "normalised_url", name="uq_source_proposal_run_url"),
        sa.CheckConstraint(
            "status IN ('proposed','accepted','rejected','duplicate','invalid','auto_activated')",
            name="ck_source_proposal_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_source_proposal_confidence_range",
        ),
        sa.CheckConstraint(
            "suggested_authority_tier >= 1 AND suggested_authority_tier <= 7",
            name="ck_source_proposal_authority_tier_range",
        ),
    )
    op.create_index("ix_source_proposal_run_id", "source_proposal", ["run_id"])
    op.create_index("ix_source_proposal_status", "source_proposal", ["status"])
    op.create_index("ix_source_proposal_normalised_url", "source_proposal", ["normalised_url"])


def downgrade() -> None:
    op.drop_table("source_proposal")
    op.drop_table("ai_invocation")
    op.drop_table("research_run")
    op.drop_table("research_plan")
