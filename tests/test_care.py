from datetime import UTC, date, datetime

from app.care import (
    care_opportunity_decision,
    classify_care_planning,
    classify_care_recruitment,
    enrich_care_signal,
)
from app.care_backfill import backfill_care_from_stored_evidence
from app.config import Settings
from app.planning import PlanningRecord, candidate_decision
from app.recruitment import RecruitmentRecord


def planning(description: str, *, postcode: str = "CV1 2AB") -> PlanningRecord:
    return PlanningRecord(
        provider="plota",
        application_id="CARE-1",
        application_url="https://example.test/planning/CARE-1",
        description=description,
        address="14 Example Road, Coventry",
        postcode=postcode,
        latitude=None,
        longitude=None,
        application_date=date(2026, 9, 20),
        status="Pending",
        decision=None,
        decision_date=None,
        council="Coventry",
        applicant="Acme Care Ltd",
        agent=None,
        raw={"id": "CARE-1", "description": description},
    )


def vacancy(title: str, description: str, employer: str = "Acme Care Ltd") -> RecruitmentRecord:
    return RecruitmentRecord(
        provider="govuk-apprenticeships",
        external_id="VAC-1",
        source_url="https://example.test/jobs/VAC-1",
        title=title,
        employer_name=employer,
        workplace_name="Acme Children's Home",
        address="14 Example Road",
        postcode="CV1 2AB",
        locality="Coventry",
        region="West Midlands",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        expires_at=None,
        salary=None,
        description=description,
        employment_type="Full time",
        raw={},
    )


def test_clear_new_childrens_home_is_opening_candidate():
    result = classify_care_planning(
        planning("Change of use from dwellinghouse to children's home for up to 3 children")
    )
    assert result.matched is True
    assert result.change_type == "OPENING"
    assert result.confidence >= 0.8


def test_childrens_home_capacity_increase_is_expansion():
    result = classify_care_planning(
        planning("Increase occupancy of existing children's home from 3 to 5 young people")
    )
    assert result.matched is True
    assert result.change_type == "EXPANSION"


def test_planning_reference_without_material_change_supports_only():
    record = planning("Existing children's home at 14 Example Road")
    raw = {
        "id": "00000000-0000-0000-0000-000000000004",
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "source_url": record.application_url,
        "external_id": "plota:CARE-1",
        "title": record.description,
        "raw_text": record.description,
        "location_hint": record.address,
        "organisation_hint": record.applicant,
        "metadata": {"provider_record": record.raw, "postcode": record.postcode},
    }
    result = enrich_care_signal(raw)
    assert result["extracted_facts"]["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"
    assert result["extracted_facts"]["commercial_change_evidence"] == "NONE"


def test_adult_care_generic_c2_and_day_nursery_are_not_care_candidates():
    assert not classify_care_planning(planning("New 60-bed elderly care home (Class C2)")).matched
    assert not classify_care_planning(planning("Change of use to a Class C2 institution")).matched
    assert not classify_care_planning(planning("Change of use to a children's day nursery")).matched


def test_one_provider_record_can_classify_independently_for_two_verticals():
    record = planning(
        "Mixed development creating a children's residential home and a new day nursery"
    )
    assert classify_care_planning(record).matched is True
    assert candidate_decision(record).matched is True


def test_routine_registered_manager_supports_only():
    result = classify_care_recruitment(
        vacancy(
            "Registered Manager — Children's Home",
            "Manager required for our established residential children's home.",
        )
    )
    assert result["matched"] is True
    assert result["relevance"] == "RELEVANT_ROUTINE"
    assert result["commercial_change_evidence"] == "NONE"


def test_brand_new_home_manager_is_change_signal():
    result = classify_care_recruitment(
        vacancy(
            "Registered Manager — Children's Home",
            "Lead our brand-new children's home through Ofsted registration and opening.",
        )
    )
    assert result["relevance"] == "RELEVANT_CHANGE"
    assert result["commercial_change_evidence"] == "STRONG"


def test_support_worker_at_existing_home_is_routine_and_adult_role_is_irrelevant():
    routine = classify_care_recruitment(
        vacancy(
            "Residential Support Worker — Children's Home",
            "Join our established residential children's home.",
        )
    )
    adult = classify_care_recruitment(
        vacancy("Registered Manager", "Registered manager for an elderly nursing care home.")
    )
    assert routine["relevance"] == "RELEVANT_ROUTINE"
    assert adult["relevance"] == "IRRELEVANT"


def test_care_enrichment_keeps_routine_recruitment_from_creating_opportunity():
    record = vacancy(
        "Registered Manager — Children's Home",
        "Manager required for our established residential children's home.",
    )
    raw = {
        "id": "00000000-0000-0000-0000-000000000001",
        "vertical": "CHILDRENS_HOME",
        "source_type": "recruitment",
        "source_url": record.source_url,
        "external_id": "gov:VAC-1",
        "title": record.title,
        "raw_text": record.description,
        "location_hint": "14 Example Road, CV1 2AB, Coventry",
        "organisation_hint": record.employer_name,
        "metadata": {
            "provider_record": {
                "vacancyReference": "VAC-1",
                "title": record.title,
                "description": record.description,
                "employer": {"name": record.employer_name},
                "addresses": [{"addressLine1": "14 Example Road", "postcode": "CV1 2AB"}],
            }
        },
    }
    candidate = enrich_care_signal(raw)
    assert candidate["extracted_facts"]["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"
    assert care_opportunity_decision(candidate).decision == "SUPPORT_EXISTING_ONLY"
    assert candidate["extracted_facts"]["location_sensitivity"] == "INTERNAL_EXACT"


def test_care_backfill_is_bounded_and_uses_stored_evidence(monkeypatch):
    source = {
        "id": "00000000-0000-0000-0000-000000000002",
        "vertical": "NURSERY",
        "schema_version": "1.0",
        "source_type": "planning",
        "source_url": "https://example.test/planning/CARE-1",
        "external_id": "plota:CARE-1",
        "discovered_at": datetime(2026, 9, 20, tzinfo=UTC),
        "title": "Change of use to children's home",
        "raw_text": "Change of use from dwellinghouse to children's home for 3 children",
        "location_hint": "14 Example Road, Coventry",
        "organisation_hint": "Acme Care Ltd",
        "metadata": {
            "provider_record": {
                "id": "CARE-1",
                "description": "Change of use from dwellinghouse to children's home for 3 children",
                "address": "14 Example Road, Coventry",
                "postcode": "CV1 2AB",
                "links": {"council": "https://example.test/planning/CARE-1"},
            }
        },
    }
    monkeypatch.setattr(
        "app.care_backfill.list_vertical_backfill_candidates", lambda *args, **kwargs: [source]
    )
    accepted = type(
        "Result",
        (),
        {"status": "accepted", "signal_id": "00000000-0000-0000-0000-000000000003"},
    )()
    monkeypatch.setattr(
        "app.care_backfill.ingest_signal", lambda *args, **kwargs: accepted
    )
    monkeypatch.setattr("app.care_backfill.record_admin_audit", lambda *args, **kwargs: "audit-1")
    result = backfill_care_from_stored_evidence(
        Settings(evidence_bucket="bucket"), actor="admin", days=365, limit=500
    )
    assert result["days"] == 90
    assert result["limit"] == 50
    assert result["evaluated"] == 1
    assert result["relevant"] == 1
    assert result["accepted"] == 1
    assert result["change_signals"] == 1
