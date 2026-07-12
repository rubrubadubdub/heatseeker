"""Source Scout plans, provider adapters, ingestion, scheduling, and UI."""

import json
from datetime import timedelta

from fastapi.testclient import TestClient
from heatseeker_ai.contracts import CandidateSource, SourceExpansionResult
from heatseeker_ai.models import (
    ActivationMode,
    AIInvocation,
    ProposalStatus,
    ResearchPlan,
    ResearchRun,
    ScoutRunStatus,
    SourceProposal,
)
from heatseeker_ai.providers import ClaudeProvider, CodexProvider, ProviderResult
from heatseeker_ai.service import (
    create_run,
    enqueue_due_plans,
    execute_run,
    reconcile_terminal_runs,
)
from heatseeker_api.main import create_app
from heatseeker_common import jobs
from heatseeker_common.db import session_scope
from heatseeker_common.migrate import upgrade_to_head
from heatseeker_common.models import Job
from heatseeker_common.timeutil import utc_now
from heatseeker_source_registry.models import ResearchScope, SourceCoverage, SourceDefinition
from sqlalchemy import func, select


class FakeProvider:
    name = "fake"

    def __init__(self, url: str = "https://new-source.example/"):
        self.url = url

    def complete(self, prompt, *, model, budgets, cancelled):
        assert "HeatSeeker Source Scout" in prompt
        assert not cancelled()
        output = SourceExpansionResult(
            summary="Found a useful directory.",
            queries_used=["scaffolding directory AU"],
            candidates=[
                CandidateSource(
                    name="New Industry Directory",
                    url=self.url,
                    source_category="industry_directory",
                    access_method="html",
                    reasoning="Lists relevant suppliers in the requested region.",
                    confidence=0.84,
                    authority_tier=3,
                    originating_query="scaffolding directory AU",
                    supporting_urls=["https://search.example/result"],
                )
            ],
            coverage_gaps=["No Tasmania-specific directory found"],
            explicit_unknowns=["Terms require deterministic review"],
        )
        return ProviderResult(output=output, raw_output=output.model_dump_json())


def _plan(session, **overrides) -> ResearchPlan:
    values = {
        "name": f"Scout {utc_now().timestamp()}",
        "provider": "codex",
        "search_config": {"keywords": ["scaffolding"]},
        "instructions": "Prioritise public directories.",
        "budgets": {"max_candidates": 10, "timeout_seconds": 30},
        "activation_mode": ActivationMode.PROPOSAL_ONLY,
    }
    values.update(overrides)
    plan = ResearchPlan(**values)
    session.add(plan)
    session.flush()
    return plan


def test_fake_provider_run_creates_audited_proposal_with_scope(engine, settings):
    with session_scope(engine) as session:
        scope = ResearchScope(
            name="Scout AU",
            geo_codes=["AU"],
            industry_ids=["scaffolding"],
            include_unknown=False,
        )
        session.add(scope)
        session.flush()
        plan = _plan(session, scope_id=scope.id)
        run = create_run(session, plan, trigger="manual", actor="test")
        run_id = run.id

    result = execute_run(engine, settings, run_id, provider=FakeProvider())

    assert result["proposed"] == 1
    with session_scope(engine) as session:
        run = session.get(ResearchRun, run_id)
        proposal = session.scalars(
            select(SourceProposal).where(SourceProposal.run_id == run_id)
        ).one()
        source = session.get(SourceDefinition, proposal.source_definition_id)
        invocation = session.scalars(
            select(AIInvocation).where(AIInvocation.run_id == run_id)
        ).one()
        coverage = session.scalars(
            select(SourceCoverage).where(SourceCoverage.source_definition_id == source.id)
        ).one()

        assert run.status == ScoutRunStatus.SUCCEEDED
        assert proposal.status == ProposalStatus.PROPOSED
        assert source.lifecycle_status == "proposed"
        assert source.authority_tier == 6
        assert {target.dimension for target in coverage.targets} == {"industry", "region"}
        assert invocation.validation_status == "valid"
        assert invocation.input_hash


def test_existing_source_is_recorded_as_duplicate(engine, settings):
    with session_scope(engine) as session:
        first = _plan(session, name="First")
        first_run = create_run(session, first, trigger="manual", actor="test")
        first_id = first_run.id
    execute_run(engine, settings, first_id, provider=FakeProvider())

    with session_scope(engine) as session:
        second = _plan(session, name="Second")
        second_run = create_run(session, second, trigger="manual", actor="test")
        second_id = second_run.id
    result = execute_run(engine, settings, second_id, provider=FakeProvider())

    assert result["duplicate"] == 1
    with session_scope(engine) as session:
        proposal = session.scalars(
            select(SourceProposal).where(SourceProposal.run_id == second_id)
        ).one()
        assert proposal.status == ProposalStatus.DUPLICATE
        assert session.scalar(select(func.count(SourceDefinition.id))) == 1


def test_due_plan_is_snapshotted_and_rescheduled(engine):
    with session_scope(engine) as session:
        plan = _plan(
            session,
            name="Scheduled",
            schedule_enabled=True,
            interval_minutes=60,
            next_run_at=utc_now() - timedelta(minutes=1),
        )
        plan_id = plan.id

    with session_scope(engine) as session:
        assert enqueue_due_plans(session) == 1

    with session_scope(engine) as session:
        plan = session.get(ResearchPlan, plan_id)
        run = session.scalars(select(ResearchRun).where(ResearchRun.plan_id == plan_id)).one()
        assert run.trigger == "schedule"
        assert run.job_id is not None
        assert plan.next_run_at > utc_now()
        assert session.get(Job, run.job_id).job_type == "source_scout.run"

        plan.next_run_at = utc_now() - timedelta(minutes=1)

    with session_scope(engine) as session:
        assert enqueue_due_plans(session) == 0
        assert session.scalar(select(func.count(ResearchRun.id))) == 1


def test_cancelled_job_reconciles_queued_run(engine):
    with session_scope(engine) as session:
        plan = _plan(session, name="Cancelled")
        run = create_run(session, plan, trigger="manual", actor="test")
        run_id = run.id
        assert jobs.cancel(session, run.job_id, actor="test")

    with session_scope(engine) as session:
        assert reconcile_terminal_runs(session) == 1

    with session_scope(engine) as session:
        run = session.get(ResearchRun, run_id)
        assert run.status == ScoutRunStatus.CANCELLED


def test_auto_activate_manual_source_without_network(engine, settings):
    class ManualProvider(FakeProvider):
        def complete(self, prompt, *, model, budgets, cancelled):
            response = super().complete(prompt, model=model, budgets=budgets, cancelled=cancelled)
            candidate = response.output.candidates[0]
            candidate.access_method = "manual"
            return response

    with session_scope(engine) as session:
        plan = _plan(session, activation_mode=ActivationMode.AUTO_ACTIVATE)
        run = create_run(session, plan, trigger="manual", actor="test")
        run_id = run.id

    result = execute_run(engine, settings, run_id, provider=ManualProvider())

    assert result["auto_activated"] == 1
    with session_scope(engine) as session:
        proposal = session.scalars(select(SourceProposal)).one()
        source = session.get(SourceDefinition, proposal.source_definition_id)
        assert proposal.status == ProposalStatus.AUTO_ACTIVATED
        assert source.lifecycle_status == "active"


def test_code_filters_blocked_domains_after_provider_output(engine, settings):
    with session_scope(engine) as session:
        plan = _plan(
            session,
            search_config={
                "keywords": ["scaffolding"],
                "blocked_domains": ["new-source.example"],
            },
        )
        run = create_run(session, plan, trigger="manual", actor="test")
        run_id = run.id

    result = execute_run(engine, settings, run_id, provider=FakeProvider())

    assert result["invalid"] == 1
    with session_scope(engine) as session:
        assert session.scalar(select(func.count(SourceDefinition.id))) == 0


def test_codex_adapter_uses_search_readonly_ephemeral_and_schema(settings, monkeypatch):
    captured = {}
    output = SourceExpansionResult(summary="none").model_dump_json()

    monkeypatch.setattr("heatseeker_ai.providers._resolve_command", lambda _command: "codex")

    def fake_run(command, prompt, *, cwd, **kwargs):
        captured["command"] = command
        captured["prompt"] = prompt
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(output)
        return "events", ""

    monkeypatch.setattr("heatseeker_ai.providers._run_agent", fake_run)
    result = CodexProvider(settings).complete(
        "research", model="test-model", budgets={}, cancelled=lambda: False
    )

    assert result.output.summary == "none"
    assert "--search" in captured["command"]
    assert "--ephemeral" in captured["command"]
    assert 'shell_environment_policy.inherit="none"' in captured["command"]
    assert captured["command"][
        captured["command"].index("--sandbox") : captured["command"].index("--sandbox") + 2
    ] == ["--sandbox", "read-only"]
    assert captured["command"][-1] == "-"


def test_claude_adapter_restricts_tools_and_reads_structured_wrapper(settings, monkeypatch):
    captured = {}
    structured = SourceExpansionResult(summary="none").model_dump(mode="json")
    wrapper = json.dumps(
        {
            "structured_output": structured,
            "usage": {"input_tokens": 12, "output_tokens": 4},
            "total_cost_usd": 0.01,
        }
    )
    monkeypatch.setattr("heatseeker_ai.providers._resolve_command", lambda _command: "claude")

    def fake_run(command, prompt, *, cwd, **kwargs):
        captured["command"] = command
        return wrapper, ""

    monkeypatch.setattr("heatseeker_ai.providers._run_agent", fake_run)
    result = ClaudeProvider(settings).complete(
        "research", model="sonnet", budgets={}, cancelled=lambda: False
    )

    assert result.output.summary == "none"
    assert result.input_tokens == 12
    assert "--safe-mode" in captured["command"]
    assert captured["command"][captured["command"].index("--tools") + 1] == "WebSearch,WebFetch"
    assert "--allowedTools" in captured["command"]
    assert "--json-schema" in captured["command"]


def test_source_scout_control_panel_creates_and_queues_plan(settings, monkeypatch):
    monkeypatch.setattr("heatseeker_api.ui_ai.all_provider_health", lambda _settings: [])
    upgrade_to_head(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/source-scout")
        assert page.status_code == 200
        assert "Source Scout" in page.text
        response = client.post(
            "/source-scout/plans",
            data={
                "name": "GUI Scout",
                "provider": "codex",
                "keywords": "scaffolding, temporary works",
                "max_candidates": "12",
                "max_turns": "4",
                "max_budget_usd": "2.5",
                "timeout_seconds": "120",
                "activation_mode": "proposal_only",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        with session_scope(app.state.engine) as session:
            plan = session.scalars(
                select(ResearchPlan).where(ResearchPlan.name == "GUI Scout")
            ).one()
            plan_id = plan.id
            assert plan.search_config["keywords"] == ["scaffolding", "temporary works"]

        queued = client.post(f"/source-scout/plans/{plan_id}/run", follow_redirects=False)
        assert queued.status_code == 303
        assert "/source-scout/runs/" in queued.headers["location"]
