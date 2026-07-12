"""Evidence chain: observations → reconciled fact assertions (M5, spec §13.14-§13.15, §17)."""

from datetime import timedelta

from heatseeker_common.db import session_scope
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.resolution import perform_merge
from heatseeker_intelligence import confidence as conf
from heatseeker_intelligence import facts
from heatseeker_intelligence.models import ExtractionMethod, FactStatus
from heatseeker_intelligence.observations import record_observation
from heatseeker_source_registry.models import SourceDefinition, SourceDocument


def make_source(session, name, *, category="company_website", tier=4) -> SourceDefinition:
    source = SourceDefinition(
        name=name,
        source_category=category,
        access_method="manual",
        authority_tier=tier,
        lifecycle_status="active",
    )
    session.add(source)
    session.flush()
    return source


def make_document(session, source: SourceDefinition, url_suffix="page") -> SourceDocument:
    document = SourceDocument(
        source_definition_id=source.id,
        source_url=f"https://example.test/{source.id}/{url_suffix}",
        content_hash="0" * 64,
        size_bytes=10,
        raw_storage_path=f"aa/bb/{source.id}-{url_suffix}",
        collector_version="test/1",
    )
    session.add(document)
    session.flush()
    return document


def test_confidence_components_and_vocabulary():
    assert conf.authority_score(1, None, "phone") == 1.0
    assert conf.authority_score(7, None, "phone") < 0.6
    # Question-relative bonus (§17.3): registry beats its raw tier for registration facts.
    plain = conf.authority_score(3, "news", "registration_status")
    boosted = conf.authority_score(3, "government_registry", "registration_status")
    assert boosted > plain

    now = utc_now()
    fresh = conf.freshness_score("phone", now, now)
    old = conf.freshness_score("phone", now - timedelta(days=365), now)
    assert fresh == 1.0 and abs(old - 0.5) < 0.01
    assert conf.freshness_score("project_participation", now - timedelta(days=3650), now) == 1.0

    assert conf.corroboration_score(1) == 1.0
    assert conf.corroboration_score(3) > conf.corroboration_score(2) > 1.0
    assert conf.contradiction_score(3, 0) == 1.0
    assert conf.contradiction_score(2, 2) < 1.0

    assert conf.vocabulary(0.9) == "high"
    assert conf.vocabulary(0.6) == "moderate"
    assert conf.vocabulary(0.9, conflicted=True) == "conflicted"
    assert conf.vocabulary(0.9, stale=True) == "stale"
    assert conf.vocabulary(0.0, has_evidence=False) == "unknown"


def test_missing_stays_missing(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        assert facts.reconcile(session, org.id, "phone") is None
        assert facts.assertions_for(session, org.id) == []


def test_corroboration_across_independent_sources(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        site = make_document(session, make_source(session, "Acme site", tier=4))
        registry = make_document(
            session, make_source(session, "ABR", category="government_registry", tier=1)
        )
        for document in (site, registry):
            record_observation(
                session,
                document,
                "phone",
                "+61 7 3333 1111",
                subject_entity_id=org.id,
                extraction_confidence=0.9,
            )
        single = facts.reconcile(session, org.id, "phone")
        assert single.independent_source_count == 2
        assert single.corroboration_score > 1.0
        assert single.status in (FactStatus.CONFIRMED, FactStatus.PROBABLE)
        assert single.best_evidence_document_id  # inspectable best evidence
        assert single.rule_version == conf.RULE_VERSION

        # Repeats from the SAME source must not corroborate (§17.5).
        record_observation(
            session, site, "email", "info@acme.test", subject_entity_id=org.id
        )
        record_observation(
            session,
            make_document(session, site.source_definition, "page2"),
            "email",
            "info@acme.test",
            subject_entity_id=org.id,
        )
        email = facts.reconcile(session, org.id, "email")
        assert email.independent_source_count == 1
        assert email.corroboration_score == 1.0


def test_contradiction_is_preserved_and_flagged(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        site = make_document(session, make_source(session, "Acme site"))
        directory = make_document(
            session, make_source(session, "Dir", category="directory", tier=6)
        )
        record_observation(
            session, site, "employee_count_band", "20-49", subject_entity_id=org.id
        )
        record_observation(
            session, directory, "employee_count_band", "5-19", subject_entity_id=org.id
        )
        assertion = facts.reconcile(session, org.id, "employee_count_band")
        assert assertion.status == FactStatus.CONFLICTED
        assert assertion.confidence_vocabulary == "conflicted"
        assert len(assertion.supporting_observation_ids) == 1
        assert len(assertion.contradicting_observation_ids) == 1  # preserved, not deleted
        assert assertion.contradiction_score < 1.0


def test_stale_facts_are_flagged(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        document = make_document(session, make_source(session, "Acme site"))
        record_observation(
            session,
            document,
            "phone",
            "+61 7 3333 1111",
            subject_entity_id=org.id,
            observed_at=utc_now() - timedelta(days=365 * 4),
        )
        assertion = facts.reconcile(session, org.id, "phone")
        assert assertion.status == FactStatus.STALE
        assert assertion.confidence_vocabulary == "stale"


def test_manual_observation_counts_as_verified(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        document = make_document(session, make_source(session, "Manual", category="manual"))
        record_observation(
            session,
            document,
            "canonical_name",
            "Acme Scaffolding",
            subject_entity_id=org.id,
            extraction_method=ExtractionMethod.MANUAL,
            extraction_confidence=1.0,
        )
        assertion = facts.reconcile(session, org.id, "canonical_name")
        assert assertion.status == FactStatus.CONFIRMED
        assert assertion.confidence_vocabulary == "verified"


def test_reconcile_spans_merge_group(engine):
    with session_scope(engine) as session:
        survivor = entities.create_organisation(session, "Acme Scaffolding Pty Ltd")
        absorbed = entities.create_organisation(session, "Acme Scaffold Hire")
        document = make_document(session, make_source(session, "Acme site"))
        record_observation(
            session, document, "phone", "+61 7 3333 1111", subject_entity_id=absorbed.id
        )
        perform_merge(session, survivor.id, absorbed.id, rationale="same business")
        assertion = facts.reconcile(session, survivor.id, "phone")
        # Evidence recorded against the absorbed record still feeds the canonical fact.
        assert assertion is not None
        assert assertion.subject_entity_id == survivor.id
