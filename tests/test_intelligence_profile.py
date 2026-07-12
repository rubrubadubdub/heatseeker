"""Classification, capabilities, sizing, gaps, and profile assembly (M5, spec §15-§18)."""


from heatseeker_common.db import session_scope
from heatseeker_common.timeutil import utc_now
from heatseeker_entity_resolution import entities
from heatseeker_intelligence import (
    capabilities,
    classifications,
    facts,
    gaps,
    profile,
    sizing,
)
from heatseeker_intelligence.models import (
    AssignmentType,
    CapabilityStatus,
    QuestionStatus,
    SizeConcept,
)
from heatseeker_intelligence.observations import (
    PREDICATE_EMPLOYEES,
    PREDICATE_SERVICE_CLAIM,
    record_observation,
)
from test_intelligence_facts import make_document, make_source


def test_classification_assignment_types_and_explainability(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        site = make_document(session, make_source(session, "Acme site"))
        observation = record_observation(
            session,
            site,
            PREDICATE_SERVICE_CLAIM,
            "scaffold_design",
            subject_entity_id=org.id,
        )
        results = classifications.classify_from_observations(
            session,
            org.id,
            [observation],
            pack_id="scaffolding_anz",
            source_category="company_website",
            known_service_ids={"scaffold_design": "Scaffold design"},
        )
        assert len(results) == 1
        assignment = results[0]
        # Explainability (§15.3): claimed-vs-inferred + evidence + version all present.
        assert assignment.assignment_type == AssignmentType.SELF_DESCRIBED
        assert assignment.evidence_ids == [observation.id]
        assert assignment.classifier_version == classifications.CLASSIFIER_VERSION
        assert assignment.category_label == "Scaffold design"

        # Claims outside the pack vocabulary are ignored, never guessed (§40).
        bogus = record_observation(
            session, site, PREDICATE_SERVICE_CLAIM, "quantum_llm_ops", subject_entity_id=org.id
        )
        assert (
            classifications.classify_from_observations(
                session,
                org.id,
                [bogus],
                pack_id="scaffolding_anz",
                source_category="company_website",
                known_service_ids={"scaffold_design": "Scaffold design"},
            )
            == []
        )

        # A human rejection sticks even when new evidence arrives.
        classifications.reject(session, assignment.id)
        again = classifications.assign(
            session,
            org.id,
            pack_id="scaffolding_anz",
            taxonomy_id="service_taxonomy",
            category_id="scaffold_design",
            assignment_type=AssignmentType.OBSERVED,
            confidence=0.9,
        )
        assert again.assignment_type == AssignmentType.REJECTED
        assert classifications.classifications_for(session, [org.id]) == []


def test_capability_ladder(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        own_site = make_source(session, "Acme site", category="company_website")
        project_page = make_source(session, "Project page", category="project_registry", tier=3)
        second = make_source(session, "Award notice", category="government_registry", tier=2)

        def add_evidence(source, suffix, contradicts=False):
            document = make_document(session, source, suffix)
            observation = record_observation(
                session, document, PREDICATE_SERVICE_CLAIM, "scaffold_design",
                subject_entity_id=org.id,
            )
            return capabilities.record_capability_evidence(
                session,
                org.id,
                pack_id="scaffolding_anz",
                capability_id="scaffold_design",
                capability_label="Scaffold design",
                observation_id=observation.id,
                source_definition_id=source.id,
                source_category=source.source_category,
                observed_at=utc_now(),
                contradicts=contradicts,
            )

        # Self-description alone: claimed, never more (§39.2).
        assignment = add_evidence(own_site, "a")
        assert assignment.capability_status == CapabilityStatus.CLAIMED

        # One independent source: evidenced.
        assignment = add_evidence(project_page, "b")
        assert assignment.capability_status == CapabilityStatus.EVIDENCED

        # Three pieces across two independent sources: repeatedly evidenced.
        assignment = add_evidence(second, "c")
        assert assignment.capability_status == CapabilityStatus.REPEATEDLY_EVIDENCED
        assert assignment.evidence_strength >= 0.85

        # A contradicting observation parks it (§17.6).
        assignment = add_evidence(project_page, "d", contradicts=True)
        assert assignment.capability_status == CapabilityStatus.CONTRADICTED

        # Only a human can mark verified; verified is sticky.
        capabilities.verify_capability(session, assignment.id)
        capabilities.refresh_status(assignment)
        assert assignment.capability_status == CapabilityStatus.VERIFIED


def test_sizing_never_fabricates(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        estimates = sizing.estimate_sizes(session, org.id)
        # No evidence at all → every concept unresolved with empty basis (§16.1).
        assert all(e.band == "unresolved" for e in estimates.values())
        assert all(e.confidence == 0.0 for e in estimates.values())

        document = make_document(
            session, make_source(session, "ABR", category="government_registry", tier=1)
        )
        record_observation(
            session, document, PREDICATE_EMPLOYEES, "20-49", subject_entity_id=org.id
        )
        facts.reconcile(session, org.id, PREDICATE_EMPLOYEES)
        entities.add_unit(session, org, name="Northside yard")
        entities.add_unit(session, org, name="Southside yard")
        estimates = sizing.estimate_sizes(session, org.id)

        legal = estimates[SizeConcept.LEGAL_ENTITY_SIZE]
        assert legal.band == "20-49"
        assert legal.basis[0]["indicator"] == "employee_count_band"
        group = estimates[SizeConcept.OPERATING_GROUP_SIZE]
        assert group.band == "multi-site"  # separate concept, separate evidence (§16.3)
        # Branch-level data doesn't exist, so branch size stays honest.
        assert estimates[SizeConcept.LOCAL_BRANCH_SIZE].band == "unresolved"


def test_gaps_generated_and_deduped(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Mystery Co")
        created = gaps.generate_for(session, org.id)
        types = {q.question_type for q in created}
        assert {
            "missing_identifier",
            "missing_domain",
            "missing_location",
            "missing_contact",
            "no_capability_evidence",
        } <= types

        # Regeneration does not duplicate open questions.
        assert gaps.generate_for(session, org.id) == []

        # Conflicts spawn questions (§17.6).
        site = make_document(session, make_source(session, "Site"))
        directory = make_document(
            session, make_source(session, "Dir", category="directory", tier=6)
        )
        record_observation(session, site, "phone", "111", subject_entity_id=org.id)
        record_observation(session, directory, "phone", "222", subject_entity_id=org.id)
        facts.reconcile(session, org.id, "phone")
        created = gaps.generate_for(session, org.id)
        assert any(q.question_type == "conflicted_fact:phone" for q in created)

        # Resolving removes it from the open queue.
        question = created[0]
        gaps.resolve_question(session, question.id, status=QuestionStatus.RESOLVED, note="fixed")
        open_types = {q.question_type for q in gaps.open_questions(session, [org.id])}
        assert question.question_type not in open_types


def test_profile_exposes_evidence_confidence_and_conflicts(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(session, "Acme Scaffolding")
        site = make_document(session, make_source(session, "Acme site"))
        registry = make_document(
            session, make_source(session, "ABR", category="government_registry", tier=1)
        )
        record_observation(session, site, "phone", "+61733331111", subject_entity_id=org.id)
        record_observation(session, registry, "phone", "+61733331111", subject_entity_id=org.id)
        record_observation(
            session, site, "employee_count_band", "20-49", subject_entity_id=org.id
        )
        record_observation(
            session, registry, "employee_count_band", "5-19", subject_entity_id=org.id
        )
        profile.refresh(session, org.id)
        assembled = profile.assemble(session, org.id)

        by_predicate = {f["predicate"]: f for f in assembled["facts"]}
        phone = by_predicate["phone"]
        # Every field row exposes numeric + vocabulary confidence and its components.
        assert set(phone["components"]) == {
            "authority", "extraction", "match", "freshness", "corroboration", "contradiction",
        }
        assert phone["confidence"] > 0
        assert phone["confidence_vocabulary"] in ("high", "moderate", "low")
        assert phone["independent_source_count"] == 2
        assert phone["best_evidence_document"] is not None  # evidence viewer link target

        # Conflicts are visible, not hidden.
        assert any(f["predicate"] == "employee_count_band" for f in assembled["conflicts"])
        assert any(
            q.question_type == "conflicted_fact:employee_count_band"
            for q in assembled["research_questions"]
        )
        # Missing stays missing: no fabricated predicates.
        assert "website_domain" not in by_predicate
        assert any(q.question_type == "missing_domain" for q in assembled["research_questions"])
