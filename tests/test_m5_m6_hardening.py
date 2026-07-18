"""Adversarial invariants added by the M5/M6 qualification hardening."""

from datetime import datetime, timedelta

import pytest
from heatseeker_common.db import session_scope
from heatseeker_common.models import AuditLog
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution import entities
from heatseeker_intelligence import gaps, pipeline
from heatseeker_intelligence.capabilities import record_capability_evidence
from heatseeker_intelligence.models import (
    CapabilityStatus,
    QuestionStatus,
    ResearchQuestion,
)
from heatseeker_intelligence.observations import record_observation
from heatseeker_knowledge_graph import graph, projects
from heatseeker_knowledge_graph.models import ParticipationStatus
from sqlalchemy import select
from test_intelligence_facts import make_document, make_source


def _evidence(session, label, *, organisation_id=None, observed_at=None):
    source = make_source(session, f"Hardening evidence {label}")
    document = make_document(session, source, label)
    observation = record_observation(
        session,
        document,
        "description",
        f"Evidence for {label}",
        subject_entity_id=organisation_id,
        observed_at=observed_at,
    )
    return source, document, observation


def test_resolved_evidence_gaps_close_automatically(engine):
    with session_scope(engine) as session:
        organisation = entities.create_organisation(session, "Gap Closure Co")
        gaps.generate_for(session, organisation.id)
        identifier_gap = session.scalar(
            select(ResearchQuestion).where(
                ResearchQuestion.entity_id == organisation.id,
                ResearchQuestion.question_type == "missing_identifier",
            )
        )
        assert identifier_gap.status == QuestionStatus.OPEN

        entities.add_identifier(session, organisation, "abn", "12345678901")
        gaps.generate_for(session, organisation.id)
        assert identifier_gap.status == QuestionStatus.RESOLVED
        assert identifier_gap.resolution["by"] == "system"


def test_pipeline_ages_capabilities_from_evidence_time_not_cache_time(engine):
    old = utc_now() - timedelta(days=4 * 365)
    with session_scope(engine) as session:
        organisation = entities.create_organisation(session, "Aged Capability Co")
        source, _document, observation = _evidence(
            session, "old-capability", organisation_id=organisation.id, observed_at=old
        )
        capability = record_capability_evidence(
            session,
            organisation.id,
            pack_id="scaffolding_anz",
            capability_id="hire",
            observation_id=observation.id,
            source_definition_id=source.id,
            source_category="industry_association",
            authority_tier=3,
            observed_at=old,
        )
        # Simulate a status cached by an older rule and touched recently by metadata.
        capability.capability_status = CapabilityStatus.EVIDENCED
        capability.updated_at = utc_now()
        session.flush()

        assert pipeline._refresh_stale_entities(session, limit=10) == 1
        assert capability.capability_status == CapabilityStatus.HISTORICAL


def test_m6_rejects_unprovable_or_overstated_edges(engine):
    with session_scope(engine) as session:
        subject, object_ = [
            entities.create_organisation(session, name)
            for name in ("Precise Subject", "Precise Object")
        ]
        with pytest.raises(ValueError, match="unknown evidence"):
            graph.add_relationship(
                session,
                subject.id,
                object_.id,
                "supplier_to",
                evidence_ids=["not-a-real-evidence-id"],
            )
        with pytest.raises(ValueError, match="requires evidence"):
            graph.add_relationship(
                session, subject.id, object_.id, "supplier_to", confidence=0.51
            )

        _source, document, _observation = _evidence(session, "real-relationship")
        edge = graph.add_relationship(
            session,
            subject.id,
            object_.id,
            "supplier_to",
            confidence=0.9,
            evidence_ids=[document.id],
            created_by="auditor",
        )
        with pytest.raises(graph.GraphError, match="must not precede"):
            graph.end_relationship(
                session, edge.id, valid_to=edge.valid_from - timedelta(seconds=1)
            )
        actions = set(session.scalars(select(AuditLog.action)).all())
        assert "relationship.created" in actions


def test_m6_unconfirmed_projects_do_not_become_graph_facts(engine):
    with session_scope(engine) as session:
        subject, peer = [
            entities.create_organisation(session, name)
            for name in ("Unconfirmed Subject", "Unconfirmed Peer")
        ]
        project = projects.create_project(session, "Unconfirmed Project")
        subject_role = projects.add_participation(
            session, project.id, subject.id, "bidder", confidence=0.5
        )
        projects.add_participation(
            session, project.id, peer.id, "principal_contractor", confidence=0.5
        )
        assert graph.edges_for(session, subject.id) == []
        with pytest.raises(ValueError, match="requires evidence"):
            projects.set_participation_status(
                session, subject_role.id, ParticipationStatus.CONFIRMED
            )

        _source, document, _observation = _evidence(session, "project-role")
        projects.set_participation_status(
            session,
            subject_role.id,
            ParticipationStatus.PROBABLE,
            evidence_ids=[document.id],
        )
        peer_role = project.participations[1]
        projects.set_participation_status(
            session,
            peer_role.id,
            ParticipationStatus.PROBABLE,
            evidence_ids=[document.id],
        )
        assert len(graph.edges_for(session, subject.id)) == 1

        projects.set_participation_status(
            session, subject_role.id, ParticipationStatus.RETRACTED
        )
        with pytest.raises(ValueError, match="history is immutable"):
            projects.set_participation_status(
                session,
                subject_role.id,
                ParticipationStatus.PROBABLE,
                evidence_ids=[document.id],
            )


def test_m6_project_values_and_dates_are_validated(engine):
    with session_scope(engine) as session:
        with pytest.raises(ValueError, match="currency is required"):
            projects.create_project(session, "Valued Project", estimated_value=1_000_000)
        with pytest.raises(ValueError, match="must not precede"):
            projects.create_project(
                session,
                "Reverse Project",
                start_date=utc_now(),
                end_date=utc_now() - timedelta(days=1),
            )
        with pytest.raises(ValueError, match="must include a timezone"):
            projects.create_project(
                session, "Naive Project", start_date=datetime(2026, 8, 1)
            )
