from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

import pytest
from app.collector import collect_planning
from app.config import Settings
from app.planning import (
    PlanningQuery,
    PlanningRateLimitError,
    PlanningRecord,
    PlotaProvider,
    candidate_decision,
    normalize_plota_record,
    planning_signal,
)


def record(description: str, *, address: str = "12 High Street, Bristol BS1 1AA") -> PlanningRecord:
    return PlanningRecord(
        provider="plota",
        application_id="abc123",
        application_url="https://council.example/app/abc123",
        description=description,
        address=address,
        postcode="BS1 1AA",
        latitude=51.45,
        longitude=-2.59,
        application_date=date(2026, 9, 23),
        status="Pending Consideration",
        decision=None,
        decision_date=None,
        council="Bristol",
        applicant="Little Acorns Ltd",
        agent=None,
        raw={"id": "abc123", "description": description},
    )


def test_scheduled_query_uses_a_bounded_recent_window() -> None:
    query = PlanningQuery.from_event({"lookback_days": 2, "max_records": 100, "page_size": 25})

    assert query.to_date >= query.from_date
    assert (query.to_date - query.from_date).days == 2
    assert query.max_records == 100
    assert query.page_size == 25


def test_query_lookback_is_clamped() -> None:
    short_query = PlanningQuery.from_event({"lookback_days": 0})
    long_query = PlanningQuery.from_event({"lookback_days": 100})

    assert (short_query.to_date - short_query.from_date).days == 1
    assert (long_query.to_date - long_query.from_date).days == 31


def test_positive_and_exclusion_matching() -> None:
    assert candidate_decision(record("Change of use to a children's day nursery")).matched
    assert candidate_decision(record("Extension to an existing Montessori nursery")).matched
    assert candidate_decision(
        record(
            "Change of use to an early years day nursery",
            address="Lady Elizabeth Hastings Primary School, LS22 5BS",
        )
    ).matched
    assert not candidate_decision(record("Extension to a plant nursery")).matched
    assert not candidate_decision(record("Create a nursery bedroom in the house")).matched
    assert not candidate_decision(record("Works to a nursery school classroom")).matched


@pytest.mark.parametrize(
    "description",
    [
        (
            "Outline application for 302 dwellings, a community hub and a new two-form "
            "entry primary and nursery."
        ),
        "Extension to existing primary school nursery provision to increase capacity.",
        "New nursery and pre-school accommodation at the primary school.",
    ],
)
def test_material_school_nursery_provision_is_a_candidate(description: str) -> None:
    decision = candidate_decision(record(description))
    assert decision.matched is True
    assert decision.school_nursery is True or "pre-school" in description


def test_incidental_school_nursery_reference_is_excluded() -> None:
    decision = candidate_decision(
        record("Residential development near an existing primary school and nursery.")
    )
    assert decision.matched is False
    assert "incidental-school-nursery-reference" in decision.exclusions


@pytest.mark.parametrize(
    "description,address",
    [
        (
            "Prior notification for an agricultural farm/forestry office and security centre.",
            "New Barn Nursery, Broadford Bridge Road, RH20 2LF",
        ),
        (
            "Prior notification of change of use of agricultural building to 4 dwellings.",
            "Barn at Springwell Nursery, Walden Road, CB10 1UE",
        ),
        (
            "Details pursuant to condition 56 on permission for 1,400 dwellings including "
            "a primary school and nursery.",
            "Phase 7 Rochester Riverside, ME1 1NH",
        ),
        (
            "Application for a Non-Material Amendment in relation to an approved nursery "
            "elevation: minor alterations to doors and windows.",
            "Land at Foxlow Farm, Harpur Hill Road",
        ),
    ],
)
def test_live_sample_false_positive_contexts_are_excluded(description: str, address: str) -> None:
    decision = candidate_decision(record(description, address=address))
    assert decision.matched is False


def test_explicit_childcare_amendment_remains_detectable() -> None:
    decision = candidate_decision(
        record(
            "Non-Material Amendment to approved Early Years Day Nursery elevations.",
            address="12 High Street, Bristol BS1 1AA",
        )
    )
    assert decision.matched is True


@pytest.mark.parametrize(
    "description",
    [
        "Community garden nursery wins award",
        "Plant nursery expansion",
        "Tree nursery planning application",
        "Garden centre nursery stock",
        "Horticultural nursery award",
    ],
)
def test_horticultural_records_are_not_planning_candidates(description: str) -> None:
    decision = candidate_decision(record(description))
    assert decision.matched is False
    assert decision.likely_false_positive is True
    assert decision.horticultural_terms


def test_candidate_matching_inspects_provider_record_text() -> None:
    item = record("Nursery expansion")
    item = replace(item, raw={"description": "Tree nursery propagation"})
    decision = candidate_decision(item)
    assert decision.matched is False
    assert decision.likely_false_positive is True


def test_regression_fixture_cases_match_expected_classification() -> None:
    fixtures = json.loads(
        Path("fixtures/classification_regressions.json").read_text(encoding="utf-8")
    )
    for fixture in fixtures:
        item = record(
            fixture["raw_text"],
            address=fixture.get("organisation_hint", "12 High Street") + ", Bristol BS1 1AA",
        )
        decision = candidate_decision(item)
        assert decision.likely_false_positive is (fixture["expected"] == "false_positive")
        if fixture["expected"] == "false_positive":
            assert decision.matched is False
        else:
            assert decision.matched is True


def test_normalization_preserves_structured_provider_fields() -> None:
    normalized = normalize_plota_record(
        {
            "id": "plota-1",
            "description": "Change of use to a day nursery",
            "address": "1 Main Street",
            "postcode": "BS1 1AA",
            "authority": {"name": "Bristol"},
            "location": {"lat": 51.45, "lng": -2.59},
            "date_received": "2026-09-23",
            "stage": "pending",
            "links": {"council": "https://council.example/planning/plota-1"},
        },
        "https://api.plota.co.uk/v1",
    )
    assert normalized.application_id == "plota-1"
    assert normalized.application_date == date(2026, 9, 23)
    assert normalized.latitude == 51.45
    assert normalized.application_url.startswith("https://council.example")


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_plota_provider_follows_cursor_pagination() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if len(requests) == 1:
            return FakeResponse(
                {
                    "data": [
                        {
                            "id": "1",
                            "description": "nursery",
                            "links": {"council": "https://council/1"},
                        }
                    ],
                    "meta": {"next_cursor": "next"},
                }
            )
        return FakeResponse(
            {
                "data": [
                    {
                        "id": "2",
                        "description": "childcare",
                        "links": {"council": "https://council/2"},
                    }
                ],
                "meta": {"next_cursor": None},
            }
        )

    provider = PlotaProvider("secret", opener=opener, sleep=lambda seconds: None)
    query = PlanningQuery(date(2026, 9, 1), date(2026, 9, 2), max_records=2)
    results = list(provider.applications(query))
    assert [item.application_id for item in results] == ["1", "2"]
    assert "cursor=next" in requests[1]


def test_planning_signal_maps_metadata_and_stable_identity() -> None:
    item = record("A new early years nursery is proposed")
    signal = planning_signal(item, candidate_decision(item))
    assert signal["external_id"] == "plota:abc123"
    assert signal["source_type"] == "planning"
    assert signal["metadata"]["council"] == "Bristol"
    assert signal["metadata"]["provider_record"]["id"] == "abc123"


def test_collector_queues_only_candidates_and_reports_counts(monkeypatch) -> None:
    logged = []
    queued = []

    class FakeProvider:
        def applications(self, query):
            yield record("Change of use to a day nursery")
            yield record("A tree nursery and garden centre")

    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    monkeypatch.setattr(
        "app.collector.logger.info", lambda message, *args: logged.append(message % args)
    )
    settings = Settings(ingestion_queue_url="https://sqs.example/ingestion")
    counts = collect_planning(
        settings,
        {"from_date": "2026-09-23", "to_date": "2026-09-24", "source": "manual"},
        FakeProvider(),
    )
    assert counts == {
        "records_fetched": 2,
        "candidates_matched": 1,
        "signals_queued": 1,
        "duplicates": 0,
        "excluded": 1,
        "errors": 0,
    }
    assert queued[0].signal["external_id"] == "plota:abc123"
    assert logged == [
        "planning_collection_summary fetched=2 matched=1 queued=1 duplicates=0 "
        "excluded=1 errors=0 lookback_days=2 max_records=100 page_size=50 source=manual"
    ]


def test_provider_invalid_shape_is_rejected() -> None:
    class InvalidResponse(FakeResponse):
        def read(self) -> bytes:
            return b"{}"

    provider = PlotaProvider("secret", opener=lambda request, timeout: InvalidResponse({}))
    with pytest.raises(Exception, match="invalid shape"):
        list(provider.applications(PlanningQuery(date(2026, 9, 1), date(2026, 9, 2))))


def test_provider_rate_limit_retries_then_reports_rate_limit() -> None:
    def opener(request, timeout):
        raise HTTPError(request.full_url, 429, "too many", {"Retry-After": "1"}, None)

    provider = PlotaProvider("secret", opener=opener, sleep=lambda seconds: None)
    with pytest.raises(PlanningRateLimitError):
        list(provider.applications(PlanningQuery(date(2026, 9, 1), date(2026, 9, 2))))
