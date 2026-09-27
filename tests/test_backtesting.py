from __future__ import annotations

from datetime import UTC, datetime

from app.backtesting import (
    BacktestBounds,
    aggregate_results,
    company_identity_available,
    compare_metrics,
    historical_availability,
    historical_signal_snapshot,
    replay_case,
    run_fingerprint,
)


def case(**overrides):
    value = {
        "id": "11111111-1111-4111-8111-111111111111",
        "benchmark_case_id": "ofsted:1234567",
        "vertical": "CHILDRENS_HOME",
        "known_operator": "Acme Care Limited",
        "known_site": "Acme Coventry",
        "known_postcode": "CV1 2AB",
        "known_company_number": "12345678",
        "known_regulatory_id": "1234567",
        "outcome_type": "REGISTERED",
        "outcome_date": "2026-09-01",
    }
    value.update(overrides)
    return value


def planning_signal(*, date_value="2026-02-14", operator="Acme Care Ltd", postcode="CV1 2AB"):
    return {
        "id": "planning-1",
        "schema_version": "1.0",
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "source_url": "https://planning.example/1",
        "external_id": "P-1",
        "discovered_at": f"{date_value}T09:00:00Z",
        "persisted_at": "2026-09-20T09:00:00Z",
        "title": "Change of use to children's home",
        "raw_text": "Change of use from dwelling to children's home for up to 3 children.",
        "location_hint": f"14 Example Road, Coventry, {postcode}",
        "organisation_hint": operator,
        "metadata": {
            "application_date": date_value,
            "postcode": postcode,
            "applicant": operator,
            "council": "Coventry",
        },
    }


def recruitment_signal(*, date_value="2026-05-22"):
    return {
        "id": "recruitment-1",
        "schema_version": "1.0",
        "vertical": "CHILDRENS_HOME",
        "source_type": "recruitment",
        "source_url": "https://jobs.example/1",
        "external_id": "R-1",
        "discovered_at": f"{date_value}T10:00:00Z",
        "title": "Registered Manager - brand-new children's home",
        "raw_text": "Lead the opening of our brand-new children's home in Coventry.",
        "location_hint": "Coventry, CV1 2AB",
        "organisation_hint": "Acme Care",
        "metadata": {
            "published_at": f"{date_value}T10:00:00Z",
            "postcode": "CV1 2AB",
            "discovery_verticals": ["CHILDRENS_HOME"],
        },
    }


def bounds(as_of="2026-09-01T23:59:59Z"):
    return BacktestBounds.from_values(as_of=as_of, lookback_days=365, max_cases=30, max_signals=500)


def test_source_availability_uses_source_date_not_later_retrieval() -> None:
    signal = planning_signal()
    assert historical_availability(signal).date().isoformat() == "2026-02-14"
    vacancy = recruitment_signal()
    assert historical_availability(vacancy).date().isoformat() == "2026-05-22"


def test_ofsted_registration_date_cannot_leak_as_publication_date() -> None:
    signal = {
        "source_type": "ofsted",
        "discovered_at": "2026-10-01T00:00:00Z",
        "persisted_at": "2026-10-01T00:00:00Z",
        "metadata": {"registration_date": "2025-01-01"},
    }
    assert historical_availability(signal).date().isoformat() == "2026-10-01"


def test_later_recruitment_and_ofsted_do_not_affect_earlier_replay() -> None:
    later = recruitment_signal(date_value="2026-10-01")
    ofsted = {
        **planning_signal(date_value="2026-03-01"),
        "id": "ofsted-1",
        "source_type": "ofsted",
        "metadata": {"ofsted_urn": "1234567", "registration_date": "2026-09-01"},
    }
    result = replay_case(case(), [planning_signal(), later, ofsted], [], bounds())
    assert result["detected"] is True
    assert all(item["source"] == "planning" for item in result["timeline"])


def test_current_companies_house_mutable_fields_are_not_replayed() -> None:
    evidence = {
        "retrieved_at": "2026-10-01T00:00:00Z",
        "safe_metadata": {
            "company_number": "12345678",
            "company_name": "ACME CARE LIMITED",
            "date_of_creation": "2025-01-01",
            "company_status": "active",
            "registered_office_address": {"postal_code": "CV1 2AB"},
        },
    }
    assert company_identity_available(evidence, datetime(2026, 9, 1, tzinfo=UTC)) is None
    safe = company_identity_available(evidence, datetime(2026, 10, 2, tzinfo=UTC))
    assert safe == {
        "operator_id": None,
        "company_number": "12345678",
        "legal_name": "ACME CARE LIMITED",
        "incorporation_date": "2025-01-01",
        "available_at": datetime(2026, 10, 1, tzinfo=UTC),
    }
    assert "company_status" not in safe
    assert "registered_office_address" not in safe


def test_current_planning_decision_state_is_masked_without_historic_snapshot() -> None:
    signal = planning_signal()
    signal["metadata"].update(
        {
            "planning_status": "REFUSED",
            "decision": "Refused in 2027",
            "decision_date": "2027-01-10",
            "provider_record": {
                "id": "P-1",
                "description": signal["raw_text"],
                "status": "Refused",
                "decision": "Refused",
                "date_received": "2026-02-14",
            },
        }
    )
    snapshot = historical_signal_snapshot(signal)
    assert "decision" not in snapshot["metadata"]
    assert "planning_status" not in snapshot["metadata"]
    assert "decision" not in snapshot["metadata"]["provider_record"]
    assert snapshot["metadata"]["provider_record"]["description"] == signal["raw_text"]


def test_planning_finds_case_before_recruitment_and_sources_are_explainable() -> None:
    result = replay_case(case(), [planning_signal(), recruitment_signal()], [], bounds())
    assert result["detected"] is True
    assert result["opportunity_created"] is True
    assert result["first_source"] == "planning"
    assert result["source_dates"]["planning"].startswith("2026-02-14")
    assert result["source_dates"]["recruitment"].startswith("2026-05-22")
    assert result["lead_time_days"] == 199
    metrics, sources = aggregate_results([result])
    assert metrics["recall"] == 1.0
    assert sources["planning"]["first_discoveries"] == 1
    assert sources["recruitment"]["corroborations"] == 1


def test_operator_name_alone_does_not_claim_a_redacted_site_outcome() -> None:
    signal = planning_signal()
    signal["metadata"]["council"] = "Coventry"
    result = replay_case(
        case(known_postcode=None, known_site=None, known_location="Lancashire"),
        [signal],
        [],
        bounds(),
    )
    assert result["usable"] is False
    assert "incomplete source history" in result["exclusion_reason"]


def test_provider_and_published_area_can_be_reported_without_claiming_exact_site() -> None:
    result = replay_case(
        case(known_postcode=None, known_site=None, known_location="Coventry"),
        [planning_signal()],
        [],
        bounds(),
    )
    assert result["detected"] is True
    assert result["site_resolution"] == "PROBABLE_PROVIDER_AREA"
    metrics, _ = aggregate_results([result])
    assert metrics["site_accuracy"] is None
    assert metrics["site_truth_cases"] == 0


def test_recruitment_can_find_change_missed_by_planning() -> None:
    result = replay_case(case(), [recruitment_signal()], [], bounds())
    assert result["detected"] is True
    assert result["first_source"] == "recruitment"
    assert result["opportunity_created"] is True


def test_ofsted_only_case_is_excluded_instead_of_counted_as_a_miss() -> None:
    ofsted = {
        **planning_signal(),
        "id": "ofsted-1",
        "source_type": "ofsted",
        "metadata": {"ofsted_urn": "1234567", "registration_date": "2026-09-01"},
    }
    result = replay_case(case(), [ofsted], [], bounds())
    assert result["usable"] is False
    assert "no historically reconstructable" in result["exclusion_reason"]


def test_admin_decisions_are_not_replay_inputs() -> None:
    signal = planning_signal()
    signal["admin_decision"] = "LINK"
    with_decision = replay_case(case(), [signal], [], bounds())
    signal.pop("admin_decision")
    without_decision = replay_case(case(), [signal], [], bounds())
    assert with_decision["timeline"] == without_decision["timeline"]


def test_same_backtest_parameters_have_stable_fingerprint() -> None:
    value = bounds()
    first = run_fingerprint(
        benchmark_version="care-ofsted-v1", vertical="CHILDRENS_HOME", bounds=value
    )
    second = run_fingerprint(
        benchmark_version="care-ofsted-v1", vertical="CHILDRENS_HOME", bounds=value
    )
    assert first == second


def test_corpus_version_changes_backtest_fingerprint() -> None:
    value = bounds()
    without_corpus = run_fingerprint(
        benchmark_version="care-ofsted-v1", vertical="CHILDRENS_HOME", bounds=value
    )
    with_corpus = run_fingerprint(
        benchmark_version="care-ofsted-v1",
        vertical="CHILDRENS_HOME",
        bounds=value,
        corpus_version="care-historical-research-v1",
    )
    assert without_corpus != with_corpus


def test_complete_case_research_turns_a_classifier_miss_into_a_usable_miss() -> None:
    irrelevant = {
        **planning_signal(),
        "title": "Retention of dwelling as C3 residential use",
        "raw_text": "Retention of a dwellinghouse in Class C3 use.",
    }
    result = replay_case(
        case(provenance={"source_coverage_complete": True}),
        [irrelevant],
        [],
        bounds(),
    )
    assert result["usable"] is True
    assert result["detected"] is False


def test_run_comparison_reports_measured_deltas_without_judging_them() -> None:
    result = compare_metrics(
        {
            "recall": 0.5,
            "precision": None,
            "lead_time_days": {"median": 90},
            "organisation_accuracy": 0.4,
            "site_accuracy": None,
            "reviews_per_genuine_opportunity": 1.5,
        },
        {
            "recall": 0.75,
            "precision": None,
            "lead_time_days": {"median": 120},
            "organisation_accuracy": 0.6,
            "site_accuracy": None,
            "reviews_per_genuine_opportunity": 1.0,
        },
    )
    assert result == {
        "recall": 0.25,
        "precision": None,
        "median_lead_time_days": 30.0,
        "organisation_accuracy": 0.2,
        "site_accuracy": None,
        "reviews_per_genuine_opportunity": -0.5,
    }


def test_unlabelled_generated_opportunities_are_not_assumed_false() -> None:
    detected = replay_case(case(), [planning_signal()], [], bounds())
    metrics, _ = aggregate_results([detected])
    assert metrics["known_false_opportunities"] == 0
    assert metrics["precision"] is None
    assert metrics["precision_limitation"] == "no reliable negative benchmark cases"


def test_reliable_negative_case_makes_labelled_precision_measurable() -> None:
    positive = replay_case(case(), [planning_signal()], [], bounds())
    negative = replay_case(
        case(
            id="22222222-2222-4222-8222-222222222222",
            benchmark_case_id="negative-1",
            outcome_type="DID_NOT_OPEN",
        ),
        [planning_signal()],
        [],
        bounds(),
    )
    metrics, _ = aggregate_results([positive, negative])
    assert metrics["known_false_opportunities"] == 1
    assert metrics["precision"] == 0.5
