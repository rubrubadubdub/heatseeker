"""Entity core: creation helpers, normalisation, search (M4)."""

import pytest
from heatseeker_common.db import session_scope
from heatseeker_entity_resolution import entities
from heatseeker_entity_resolution.models import ContactType, LocationType, UnitType
from heatseeker_entity_resolution.normalise import (
    blocking_name_tokens,
    email_domain,
    normalise_domain,
    normalise_identifier,
    normalise_name,
    phone_match_key,
)


def test_normalise_name_strips_legal_suffixes_but_keeps_brand_words():
    assert normalise_name("Acme Scaffolding Pty Ltd") == "acme scaffolding"
    assert normalise_name("ACME SCAFFOLDING LIMITED") == "acme scaffolding"
    assert normalise_name("Acme Group") == "acme group"  # brand-meaningful tail kept
    assert normalise_name("Smith & Sons Ltd") == "smith and sons"
    assert normalise_name("Ltd") == "ltd"  # never strip a name to nothing


def test_normalise_domain_and_identifier_and_phone():
    assert normalise_domain("https://www.Acme.com.au/about?x=1") == "acme.com.au"
    assert normalise_domain("ACME.com") == "acme.com"
    assert normalise_identifier("51 824 753 556") == "51824753556"
    assert phone_match_key("+61 7 3333 1111") == phone_match_key("(07) 3333 1111")
    assert phone_match_key("123") is None
    assert email_domain("Info@Acme.com.au") == "acme.com.au"
    assert email_domain("person@gmail.com") is None
    assert blocking_name_tokens("the acme and sons") == {"acme", "sons"}


def test_create_organisation_with_children_and_completeness(engine):
    with session_scope(engine) as session:
        org = entities.create_organisation(
            session,
            "Acme Scaffolding Pty Ltd",
            legal_name="Acme Scaffolding Pty Ltd",
            identifiers=[("abn", "51 824 753 556")],
            domains=["https://www.acme.com.au/"],
            description="Scaffold hire",
        )
        entities.add_contact_point(
            session, org, ContactType.GENERAL_EMAIL, "info@acme.com.au"
        )
        location = entities.add_location(
            session,
            locality="Brisbane",
            postal_code="4000",
            country="AU",
            location_type=LocationType.OFFICE,
        )
        entities.set_primary_location(session, org, location)
        unit = entities.add_unit(session, org, unit_type=UnitType.YARD, name="Northside yard")
        org_id = org.id

        assert org.identifiers[0].value_normalised == "51824753556"
        assert org.domains[0].domain == "acme.com.au"
        assert unit.organisation_id == org.id
        assert org.profile_completeness == 1.0

    with session_scope(engine) as session:
        # Re-adding the same identifier/domain must dedupe, not duplicate.
        org = entities.get_organisation(session, org_id)
        entities.add_identifier(session, org, "abn", "51-824-753-556")
        entities.add_domain(session, org, "ACME.com.au")
        assert len(org.identifiers) == 1
        assert len(org.domains) == 1


def test_blank_names_and_values_rejected(engine):
    with session_scope(engine) as session:
        with pytest.raises(ValueError):
            entities.create_organisation(session, "   ")
        org = entities.create_organisation(session, "Real Co")
        with pytest.raises(ValueError):
            entities.add_domain(session, org, "   ")


def test_contact_operational_unit_must_belong_to_same_organisation(engine):
    with session_scope(engine) as session:
        a = entities.create_organisation(session, "Acme Scaffolding")
        b = entities.create_organisation(session, "Brisbane Formwork")
        unit = entities.add_unit(session, b, name="South yard")
        with pytest.raises(ValueError, match="same organisation"):
            entities.add_contact_point(
                session,
                a,
                ContactType.PHONE,
                "07 3000 0000",
                operational_unit_id=unit.id,
            )
        with pytest.raises(ValueError, match="not found"):
            entities.add_contact_point(
                session,
                a,
                ContactType.PHONE,
                "07 3000 0000",
                operational_unit_id="missing",
            )


def test_list_organisations_searches_names_identifiers_domains(engine):
    with session_scope(engine) as session:
        entities.create_organisation(
            session, "Acme Scaffolding", identifiers=[("abn", "51824753556")]
        )
        entities.create_organisation(session, "Brisbane Formwork", domains=["bfw.com.au"])
        entities.create_organisation(session, "Unrelated Pty Ltd")

    with session_scope(engine) as session:
        assert [o.canonical_name for o in entities.list_organisations(session, query="acme")] == [
            "Acme Scaffolding"
        ]
        assert [
            o.canonical_name for o in entities.list_organisations(session, query="51824")
        ] == ["Acme Scaffolding"]
        assert [
            o.canonical_name for o in entities.list_organisations(session, query="bfw.com")
        ] == ["Brisbane Formwork"]
        assert len(entities.list_organisations(session)) == 3
        assert entities.organisation_counts(session) == {"total": 3, "merged": 0, "live": 3}
