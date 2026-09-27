import json
from datetime import UTC, date, datetime
from pathlib import Path

from app.backtesting import BacktestBounds, replay_case
from app.care import (
    care_opportunity_decision,
    classify_care_planning,
    classify_care_recruitment,
    enrich_care_signal,
)
from app.care_backfill import backfill_care_from_stored_evidence
from app.config import Settings
from app.historical_corpus import replay_signal
from app.planning import PlanningRecord, candidate_decision
from app.recruitment import RecruitmentRecord, recruitment_record_from_signal


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


def test_generic_support_worker_uses_explicit_new_home_body_context() -> None:
    result = classify_care_recruitment(
        vacancy(
            "Support Worker",
            "Support Worker required for our brand new children's home opening in Staffordshire.",
        )
    )
    assert result["role_category"] == "support_worker"
    assert result["relevance"] == "RELEVANT_CHANGE"
    assert result["commercial_change_evidence"] == "STRONG"


def test_generic_team_leader_uses_registration_stage_body_context() -> None:
    result = classify_care_recruitment(
        vacancy(
            "Team Leader",
            "Join a new residential children's home currently going through Ofsted registration.",
        )
    )
    assert result["role_category"] == "team_leader"
    assert result["relevance"] == "RELEVANT_CHANGE"
    assert "new_residential_home" in result["explicit_change_terms"]


def test_generic_titles_at_established_home_remain_routine() -> None:
    for title, description in (
        ("Support Worker", "Join our established children's home as a Support Worker."),
        (
            "Team Leader",
            "Due to staff turnover we are recruiting a Team Leader "
            "for our existing children's home.",
        ),
    ):
        result = classify_care_recruitment(vacancy(title, description))
        assert result["relevance"] == "RELEVANT_ROUTINE"
        assert result["commercial_change_evidence"] == "NONE"


def test_weak_new_and_registered_wording_do_not_imply_change() -> None:
    for description in (
        "A new opportunity to join our established Ofsted registered children's home.",
        "Join our existing children's home. Ofsted registered home experience is useful.",
        "Welcome new starters and support the home's usual opening hours.",
    ):
        result = classify_care_recruitment(vacancy("Support Worker", description))
        assert result["relevance"] == "RELEVANT_ROUTINE"
        assert result["commercial_change_evidence"] == "NONE"


def test_generic_adult_and_supported_living_roles_remain_irrelevant() -> None:
    for description in (
        "Support adults in an established supported living service.",
        "Join our elderly nursing care home as a Support Worker.",
        "A generic care role supporting adults in their own homes.",
    ):
        result = classify_care_recruitment(
            vacancy("Support Worker", description, employer="Adult Care Limited")
        )
        assert result["relevance"] == "IRRELEVANT"


def test_cumulus_historical_adverts_are_generic_change_signals() -> None:
    manifest = json.loads(
        Path("backend/app/data/care_historical_research_v1.json").read_text(encoding="utf-8")
    )
    records = [
        item for item in manifest["records"] if item["external_id"].startswith("warwickshire-")
    ]
    assert len(records) == 2
    signals = []
    for index, record in enumerate(records):
        signal = replay_signal(
            {**record, "id": f"cumulus-{index}", "corpus_version": manifest["corpus_version"]}
        )
        decision = classify_care_recruitment(recruitment_record_from_signal(signal))
        assert decision["relevance"] == "RELEVANT_CHANGE"
        assert decision["commercial_change_evidence"] == "STRONG"
        signals.append(signal)

    result = replay_case(
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "benchmark_case_id": "ofsted:2817348",
            "vertical": "CHILDRENS_HOME",
            "known_operator": "RCS Operations Limited",
            "known_location": "Warwickshire",
            "known_regulatory_id": "2817348",
            "outcome_type": "REGISTERED",
            "outcome_date": "2025-02-11",
            "provenance": {"source_coverage_complete": True},
        },
        signals,
        [],
        BacktestBounds.from_values(
            as_of="2026-09-27T23:59:59Z",
            lookback_days=365,
            max_cases=30,
            max_signals=500,
        ),
        case_signal_ids={"cumulus-0", "cumulus-1"},
    )
    assert result["detected"] is True
    assert result["first_source"] == "recruitment"
    assert result["lead_time_days"] == 97


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


def test_live_residential_childcare_wording_is_relevant_routine() -> None:
    worker = classify_care_recruitment(
        vacancy(
            "Apprentice Residential Child Care Worker",
            "Support children and young people in a residential care setting.",
        )
    )
    deputy = classify_care_recruitment(
        vacancy(
            "Apprentice Deputy Manager - Residential Childcare",
            "Support the day-to-day running of the home for children and young people.",
        )
    )
    assert worker["role_category"] == "residential_childcare_worker"
    assert worker["relevance"] == "RELEVANT_ROUTINE"
    assert worker["commercial_change_evidence"] == "NONE"
    assert deputy["role_category"] == "deputy_manager"
    assert deputy["relevance"] == "RELEVANT_ROUTINE"


def test_childrens_support_worker_is_relevant_but_generic_residential_support_is_not() -> None:
    child = classify_care_recruitment(
        vacancy(
            "Childrens Support Worker apprentice",
            "Support a child or young person while gaining a Residential Childcare qualification.",
        )
    )
    generic = classify_care_recruitment(
        RecruitmentRecord(
            provider="govuk-apprenticeships",
            external_id="VAC-GENERIC",
            source_url="https://example.test/jobs/VAC-GENERIC",
            title="Apprentice Residential Support Worker",
            employer_name="Training Provider Ltd",
            workplace_name=None,
            address="1 Example Road",
            postcode="BS4 5QU",
            locality="Bristol",
            region=None,
            published_at=datetime(2026, 9, 20, tzinfo=UTC),
            expires_at=None,
            salary=None,
            description="Support learners with day-to-day life and independence.",
            employment_type="Full time",
            raw={},
        )
    )
    assert child["role_category"] == "childrens_support_worker"
    assert child["relevance"] == "RELEVANT_ROUTINE"
    assert generic["relevance"] == "IRRELEVANT"


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
    selected = {}

    def candidates(*args, **kwargs):
        selected.update(kwargs)
        return [source]

    monkeypatch.setattr("app.care_backfill.list_vertical_backfill_candidates", candidates)
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
    assert result["planning_evaluated"] == 1
    assert result["recruitment_evaluated"] == 0
    assert selected["recruitment_discovery_vertical"] == "CHILDRENS_HOME"
