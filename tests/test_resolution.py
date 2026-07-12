"""Reversible merge/split, canonical identity, queue decisions (M4, spec §14.4-§14.5)."""

import pytest
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.matching import scan_for_duplicates
from heatseeker_entity_resolution.models import (
    EntityMatchCandidate,
    EntityMerge,
    MatchState,
    OrganisationStatus,
    UnitType,
)
from heatseeker_entity_resolution.resolution import (
    ResolutionError,
    canonical_id,
    group_profile,
    list_queue,
    merge_group,
    perform_merge,
    record_decision,
    reverse_merge,
)
from sqlalchemy import select


def _two_duplicates(session):
    a = entities.create_organisation(
        session,
        "Acme Scaffolding Pty Ltd",
        identifiers=[("abn", "51824753556")],
        domains=["acme.com.au"],
    )
    b = entities.create_organisation(
        session, "Acme Scaffold Hire", identifiers=[("abn", "51824753556")]
    )
    return a, b


def test_merge_preserves_originals_and_children(engine):
    with session_scope(engine) as session:
        survivor, absorbed = _two_duplicates(session)
        entities.add_unit(session, absorbed, unit_type=UnitType.YARD, name="Legacy yard")
        survivor_id, absorbed_id = survivor.id, absorbed.id
        perform_merge(
            session, survivor.id, absorbed.id, rationale="same ABN", performed_by="tester"
        )

    with session_scope(engine) as session:
        absorbed = entities.get_organisation(session, absorbed_id)
        # Original record intact: children still attached, nothing rewritten.
        assert absorbed.status == OrganisationStatus.MERGED
        assert absorbed.merged_into_id == survivor_id
        assert absorbed.canonical_name == "Acme Scaffold Hire"
        assert [u.name for u in absorbed.units] == ["Legacy yard"]
        assert canonical_id(session, absorbed_id) == survivor_id

        # The aggregated profile shows the group's children with per-origin attribution.
        profile = group_profile(session, absorbed_id)
        assert profile["canonical"].id == survivor_id
        assert {o.id for o in profile["group"]} == {survivor_id, absorbed_id}
        unit_origins = {row["origin"].id for row in profile["units"]}
        assert unit_origins == {absorbed_id}


def test_merge_is_exactly_reversible(engine):
    with session_scope(engine) as session:
        survivor, absorbed = _two_duplicates(session)
        absorbed_id = absorbed.id
        prior_status = absorbed.status
        survivor_observed_at = survivor.last_observed_at
        merge = perform_merge(
            session, survivor.id, absorbed.id, rationale="same ABN", performed_by="tester"
        )
        merge_id = merge.id

    with session_scope(engine) as session:
        reverse_merge(session, merge_id, reason="wrong pair", performed_by="tester")

    with session_scope(engine) as session:
        absorbed = entities.get_organisation(session, absorbed_id)
        assert absorbed.status == prior_status
        assert absorbed.merged_into_id is None
        assert canonical_id(session, absorbed_id) == absorbed_id
        merge = session.get(EntityMerge, merge_id)
        assert merge.reversed_at is not None
        assert merge.reversed_by == "tester"
        assert merge.reversal_reason == "wrong pair"
        assert merge.survivor.last_observed_at == survivor_observed_at
        # Audit row and both organisations still exist — nothing was deleted.
        assert entities.organisation_counts(session)["total"] == 2

    with session_scope(engine) as session, pytest.raises(ResolutionError):
        reverse_merge(session, merge_id, reason="again", performed_by="tester")


def test_merge_guards(engine):
    with session_scope(engine) as session:
        survivor, absorbed = _two_duplicates(session)
        parent = entities.create_organisation(session, "Acme Holdings")
        survivor.parent_organisation_id = parent.id

        with pytest.raises(ResolutionError):  # no self-merge
            perform_merge(session, survivor.id, survivor.id, rationale="x")
        with pytest.raises(ResolutionError):  # rationale mandatory
            perform_merge(session, survivor.id, absorbed.id, rationale="  ")
        with pytest.raises(ResolutionError):  # parent/subsidiary never flattened
            perform_merge(session, survivor.id, parent.id, rationale="looks similar")

        grandparent = entities.create_organisation(session, "Global Holdings")
        parent.parent_organisation_id = grandparent.id
        with pytest.raises(ResolutionError):  # transitive ancestors are protected too
            perform_merge(session, survivor.id, grandparent.id, rationale="looks similar")

        perform_merge(session, survivor.id, absorbed.id, rationale="same ABN")
        with pytest.raises(ResolutionError):  # already merged
            perform_merge(session, survivor.id, absorbed.id, rationale="again")

        # Merging *into* a merged record lands on its canonical survivor instead.
        third = entities.create_organisation(session, "Acme Scaffold Hire QLD")
        merge = perform_merge(session, absorbed.id, third.id, rationale="same business")
        assert merge.survivor_id == survivor.id


def test_chain_merge_resolves_transitively(engine):
    with session_scope(engine) as session:
        a = entities.create_organisation(session, "Acme A")
        b = entities.create_organisation(session, "Acme B")
        c = entities.create_organisation(session, "Acme C")
        a_id, b_id, c_id = a.id, b.id, c.id
        perform_merge(session, b.id, c.id, rationale="dupe")
        perform_merge(session, a.id, b.id, rationale="dupe")

    with session_scope(engine) as session:
        assert canonical_id(session, c_id) == a_id
        assert {o.id for o in merge_group(session, a_id)} == {a_id, b_id, c_id}
        merges = {
            (m.survivor_id, m.absorbed_id): m
            for m in session.execute(select(EntityMerge)).scalars()
        }
        # Reversals restore coherent state in either order: undoing a<-b splits the
        # {b, c} subgroup back out; undoing b<-c then frees c itself.
        reverse_merge(session, merges[(a_id, b_id)].id, reason="undo outer")
        assert canonical_id(session, c_id) == b_id
        assert {o.id for o in merge_group(session, b_id)} == {b_id, c_id}
        reverse_merge(session, merges[(b_id, c_id)].id, reason="undo inner")
        assert canonical_id(session, c_id) == c_id
        assert canonical_id(session, b_id) == b_id


def test_queue_decisions_and_ordering(engine):
    with session_scope(engine) as session:
        _two_duplicates(session)  # exact pair
        entities.create_organisation(session, "Brisbane Scaffold Services")
        entities.create_organisation(session, "Brisbane Scaffold Solutions")
        scan_for_duplicates(session)

    with session_scope(engine) as session:
        queue = list_queue(session)
        assert len(queue) == 2
        assert queue[0].score >= queue[1].score  # most decision-worthy first
        exact, fuzzy = queue[0], queue[1]
        assert exact.match_state == MatchState.EXACT

        merged = record_decision(
            session,
            exact.id,
            "merge",
            resolved_by="tester",
            survivor_id=exact.organisation_a_id,
        )
        assert merged.resolution == "merged"
        related = record_decision(session, fuzzy.id, "related", resolved_by="tester")
        assert related.match_state == MatchState.RELATED_BUT_DISTINCT

    with session_scope(engine) as session:
        assert list_queue(session) == []  # both decided
        # Decisions stay auditable rather than deleted.
        assert len(list_queue(session, include_resolved=True)) == 2
        with pytest.raises(ResolutionError):
            record_decision(
                session,
                list_queue(session, include_resolved=True)[0].id,
                "distinct",
                resolved_by="tester",
            )


def test_reversal_reopens_queue_candidate(engine):
    with session_scope(engine) as session:
        _two_duplicates(session)
        scan_for_duplicates(session)
        candidate = session.execute(select(EntityMatchCandidate)).scalar_one()
        original_state = candidate.match_state
        original_notes = candidate.notes
        record_decision(
            session,
            candidate.id,
            "merge",
            resolved_by="tester",
            survivor_id=candidate.organisation_a_id,
        )
        merge = session.execute(select(EntityMerge)).scalar_one()
        assert merge.signals_snapshot  # rationale evidence captured at merge time
        merge_id = merge.id

    with session_scope(engine) as session:
        reverse_merge(session, merge_id, reason="operator error", performed_by="tester")

    with session_scope(engine) as session:
        candidate = session.execute(select(EntityMatchCandidate)).scalar_one()
        assert candidate.resolution is None
        assert candidate.match_state == original_state
        assert candidate.notes == original_notes
        assert list_queue(session) != []


def test_merge_rejects_mismatched_or_decided_candidate(engine):
    with session_scope(engine) as session:
        a, b = _two_duplicates(session)
        c = entities.create_organisation(session, "Other Company")
        scan_for_duplicates(session)
        candidate = session.execute(select(EntityMatchCandidate)).scalar_one()

        with pytest.raises(ResolutionError, match="does not describe"):
            perform_merge(
                session,
                a.id,
                c.id,
                rationale="wrong queue row",
                candidate_id=candidate.id,
            )

        record_decision(session, candidate.id, "distinct", resolved_by="tester")
        with pytest.raises(ResolutionError, match="human decision"):
            perform_merge(session, a.id, b.id, rationale="bypass decision")
