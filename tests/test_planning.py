from __future__ import annotations

import json
from datetime import date
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


def test_positive_and_exclusion_matching() -> None:
    assert candidate_decision(record("Change of use to a children's day nursery")).matched
    assert candidate_decision(record("Extension to an existing Montessori nursery")).matched
    assert not candidate_decision(record("Extension to a plant nursery")).matched
    assert not candidate_decision(record("Create a nursery bedroom in the house")).matched
    assert not candidate_decision(record("Works to a nursery school classroom")).matched


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
    queued = []

    class FakeProvider:
        def applications(self, query):
            yield record("Change of use to a day nursery")
            yield record("A tree nursery and garden centre")

    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    settings = Settings(ingestion_queue_url="https://sqs.example/ingestion")
    counts = collect_planning(
        settings,
        {"from_date": "2026-09-23", "to_date": "2026-09-24"},
        FakeProvider(),
    )
    assert counts == {
        "records_fetched": 2,
        "candidates_matched": 1,
        "signals_queued": 1,
        "duplicates": 0,
        "errors": 0,
    }
    assert queued[0].signal["external_id"] == "plota:abc123"


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
