"""Match scoring and the blocked duplicate scan (M4, spec §14.2-§14.3)."""

from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.matching import (
    build_features,
    scan_for_duplicates,
    score_pair,
)
from heatseeker_entity_resolution.models import (
    ContactType,
    EntityMatchCandidate,
    MatchState,
)
from sqlalchemy import select


def _features(session, name, **kwargs):
    return build_features(entities.create_organisation(session, name, **kwargs))


def test_shared_identifier_is_exact_and_explainable(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding Pty Ltd", identifiers=[("abn", "51824753556")])
        b = _features(session, "ACME Scaffold Hire", identifiers=[("abn", "51 824 753 556")])
        state, score, signals, conflicts = score_pair(a, b)
        assert state == MatchState.EXACT
        assert score >= 0.98
        assert conflicts == 0
        assert any(s["signal"] == "shared_identifier" for s in signals)


def test_shared_domain_is_high_confidence(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding", domains=["acme.com.au"])
        b = _features(session, "Acme Hire Group", domains=["www.acme.com.au"])
        state, score, _signals, _ = score_pair(a, b)
        assert state == MatchState.HIGH_CONFIDENCE_PROBABLE
        assert score >= 0.85


def test_same_normalised_name_is_high_confidence(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding Pty Ltd")
        b = _features(session, "ACME Scaffolding Limited")
        state, _, signals, _ = score_pair(a, b)
        assert state == MatchState.HIGH_CONFIDENCE_PROBABLE
        assert any(s["signal"] == "name_exact" for s in signals)


def test_fuzzy_name_alone_only_reaches_review(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding Services")
        b = _features(session, "Acme Scaffolding Solutions")
        state, score, _, _ = score_pair(a, b)
        assert state == MatchState.POSSIBLE_REVIEW
        assert score < 0.85


def test_conflicting_identifiers_penalise_and_flag(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding", identifiers=[("abn", "11111111111")])
        b = _features(session, "Acme Scaffolding", identifiers=[("abn", "22222222222")])
        state, _score, signals, conflicts = score_pair(a, b)
        assert conflicts == 1
        assert state != MatchState.EXACT
        assert state != MatchState.HIGH_CONFIDENCE_PROBABLE
        assert any(s["signal"] == "conflicting_identifier" for s in signals)


def test_unrelated_names_produce_no_candidate(engine):
    with session_scope(engine) as session:
        a = _features(session, "Acme Scaffolding")
        b = _features(session, "Brisbane Formwork")
        state, score, _, _ = score_pair(a, b)
        assert score < 0.45
        assert state == MatchState.UNRESOLVED


def test_shared_phone_boosts_fuzzy_name(engine):
    with session_scope(engine) as session:
        org_a = entities.create_organisation(session, "Acme Scaffolding Services")
        org_b = entities.create_organisation(session, "Acme Scaffolding Solutions")
        entities.add_contact_point(session, org_a, ContactType.PHONE, "+61 7 3333 1111")
        entities.add_contact_point(session, org_b, ContactType.PHONE, "(07) 3333 1111")
        state, score, signals, _ = score_pair(build_features(org_a), build_features(org_b))
        assert state == MatchState.POSSIBLE_REVIEW
        assert any(s["signal"] == "shared_phone" for s in signals)
        assert score > 0.5


def test_scan_creates_queue_rows_and_is_idempotent(engine):
    with session_scope(engine) as session:
        entities.create_organisation(
            session, "Acme Scaffolding Pty Ltd", identifiers=[("abn", "51824753556")]
        )
        entities.create_organisation(
            session, "Acme Scaffold Hire", identifiers=[("abn", "51824753556")]
        )
        entities.create_organisation(session, "Brisbane Formwork")

    with session_scope(engine) as session:
        summary = scan_for_duplicates(session)
        assert summary["candidates_created"] == 1

    with session_scope(engine) as session:
        rows = list(session.execute(select(EntityMatchCandidate)).scalars())
        assert len(rows) == 1
        assert rows[0].match_state == MatchState.EXACT
        assert rows[0].organisation_a_id < rows[0].organisation_b_id

    with session_scope(engine) as session:
        summary = scan_for_duplicates(session)
        assert summary["candidates_created"] == 0
        assert summary["candidates_updated"] == 1
    with session_scope(engine) as session:
        assert len(list(session.execute(select(EntityMatchCandidate)).scalars())) == 1


def test_scan_skips_resolved_pairs_and_merged_orgs(engine):
    from heatseeker_entity_resolution.resolution import record_decision

    with session_scope(engine) as session:
        entities.create_organisation(session, "Acme Scaffolding", domains=["acme.com.au"])
        entities.create_organisation(session, "Acme Scaffold Group", domains=["acme.com.au"])

    with session_scope(engine) as session:
        scan_for_duplicates(session)
        candidate = session.execute(select(EntityMatchCandidate)).scalar_one()
        record_decision(session, candidate.id, "distinct", resolved_by="tester")
        resolved_at = candidate.resolved_at

    with session_scope(engine) as session:
        summary = scan_for_duplicates(session)
        assert summary["candidates_updated"] == 0
        candidate = session.execute(select(EntityMatchCandidate)).scalar_one()
        assert candidate.match_state == MatchState.CONFIRMED_DISTINCT
        assert candidate.resolved_at == resolved_at
