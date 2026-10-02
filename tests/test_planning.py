from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from app.collector import (
    collect_planning,
    recover_planning_origin,
    refresh_planning_lifecycle_watch,
)
from app.collector import handler as planning_handler
from app.config import Settings
from app.planning import (
    PlanningAuthenticationError,
    PlanningMalformedResponseError,
    PlanningProviderError,
    PlanningQuery,
    PlanningRateLimitError,
    PlanningRecord,
    PlanningReferenceSearchResult,
    PlanningTimeoutError,
    PlotaProvider,
    candidate_decision,
    normalize_plota_record,
    planning_signal,
)
from app.planning_backfill import PlanningBackfillBounds, chunk_payload


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


def test_historical_query_requires_an_explicit_weekly_range() -> None:
    query = PlanningQuery.from_historical_event(
        {"from_date": "2025-03-29", "to_date": "2025-04-04", "max_records": 9999}
    )
    assert query.from_date == date(2025, 3, 29)
    assert query.to_date == date(2025, 4, 4)
    assert query.max_records == 500
    with pytest.raises(ValueError, match="cannot exceed 7 days"):
        PlanningQuery.from_historical_event({"from_date": "2025-03-29", "to_date": "2025-04-05"})


def test_historical_backfill_is_bounded_and_chunked_without_changing_live_clamp() -> None:
    bounds = PlanningBackfillBounds.from_values(
        from_date="2025-03-29",
        to_date="2026-09-29",
        vertical="ALL",
        total_record_cap=4_000,
    )
    assert len(bounds.chunks) == 79
    assert bounds.total_record_cap == 4_000
    assert bounds.chunks[0] == (date(2025, 3, 29), date(2025, 4, 4))
    assert bounds.chunks[-1][1] == date(2026, 9, 29)
    payload = chunk_payload(
        bounds,
        backfill_id="backfill-1",
        parent_started_at="2026-09-29T12:00:00+00:00",
        chunk_index=0,
    )
    assert payload["invocation_source"] == "historical_backfill"
    assert payload["verticals"] == ["NURSERY", "CHILDRENS_HOME"]
    assert payload["from_date"] == "2025-03-29"
    assert payload["to_date"] == "2025-04-04"
    assert payload["page_size"] == 25
    with pytest.raises(ValueError, match="cannot exceed 550 days"):
        PlanningBackfillBounds.from_values(from_date="2025-01-01", to_date="2026-09-29")


def test_historical_planning_signal_preserves_source_date() -> None:
    item = record("Change of use to a day nursery")
    signal = planning_signal(item, candidate_decision(item), historical_source_date=True)
    assert signal["discovered_at"] == "2026-09-23T00:00:00+00:00"


def test_completed_backfill_chunk_is_reused_and_next_chunk_is_queued_once(
    monkeypatch,
) -> None:
    bounds = PlanningBackfillBounds.from_values(
        from_date="2026-09-01",
        to_date="2026-09-14",
        vertical="ALL",
        total_record_cap=1000,
    )
    payload = chunk_payload(
        bounds,
        backfill_id="backfill-1",
        parent_started_at="2026-09-29T12:00:00+00:00",
        chunk_index=0,
    )
    settings = Settings(
        source_runs_table_name="runs",
        planning_manual_run_queue_url="https://sqs.example/planning",
    )
    monkeypatch.setattr("app.collector.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "app.collector.get_run",
        lambda *args, **kwargs: {"status": "SUCCESS", "counts": {"records_fetched": 12}},
    )
    monkeypatch.setattr(
        "app.collector.collect_planning",
        lambda *args, **kwargs: pytest.fail("completed chunk should not be collected twice"),
    )
    monkeypatch.setattr("app.collector.update_run_progress", lambda *args, **kwargs: None)
    sent = []

    class FakeSqs:
        def send_message(self, **kwargs):
            sent.append({**kwargs, "body": json.loads(kwargs["MessageBody"])})
            return {"MessageId": "next-1"}

    monkeypatch.setattr("app.collector.boto3.client", lambda name: FakeSqs())
    result = planning_handler(payload, None)
    assert result["records_fetched"] == 12
    assert result["chunks_completed"] == 1
    assert len(sent) == 1
    assert sent[0]["DelaySeconds"] == 60
    assert sent[0]["body"]["chunk_index"] == 1
    assert sent[0]["body"]["cumulative_counts"]["records_fetched"] == 12


def test_backfill_rate_limit_stops_chain_without_sqs_redrive(monkeypatch) -> None:
    bounds = PlanningBackfillBounds.from_values(
        from_date="2026-09-01",
        to_date="2026-09-07",
        vertical="ALL",
        total_record_cap=100,
    )
    payload = chunk_payload(
        bounds,
        backfill_id="backfill-rate-limited",
        parent_started_at="2026-09-29T12:00:00+00:00",
        chunk_index=0,
    )
    monkeypatch.setattr(
        "app.collector.Settings.from_env",
        lambda: Settings(source_runs_table_name="runs"),
    )
    monkeypatch.setattr("app.collector.get_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.collector.collect_planning",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PlanningRateLimitError("Plota rate limit exceeded")
        ),
    )
    failures = []
    monkeypatch.setattr(
        "app.collector.finish_run",
        lambda *args, **kwargs: failures.append(kwargs),
    )
    result = planning_handler(payload, None)
    assert result == {"errors": 1}
    assert failures[0]["status"] == "FAILED"
    assert failures[0]["failure_category"] == "PlanningRateLimitError"


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


def test_plota_provider_reference_lookup_is_single_bounded_exact_query() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(
            {
                "data": [
                    {
                        "id": "opaque-1",
                        "reference": "24/03385/FUL",
                        "description": "children's home",
                    },
                    {
                        "id": "opaque-2",
                        "reference": "24/03386/FUL",
                        "description": "different application",
                    },
                ]
            }
        )

    provider = PlotaProvider("secret", opener=opener)
    search = provider.applications_by_reference("24 / 03385 / ful")
    assert [item.application_id for item in search.candidates] == ["opaque-1"]
    assert search.provider_query == "24 / 03385 / ful"
    assert search.truncated is False
    assert len(requests) == 1
    assert "reference=24+%2F+03385+%2F+ful" in requests[0]
    assert "q=" not in requests[0]
    assert "council=" not in requests[0]


def test_plota_provider_retrieves_known_application_by_stable_id() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(
            {
                "data": {
                    "id": "stable-123",
                    "reference": "24/03385/FUL",
                    "description": "children's home",
                    "authority": {"name": "Croydon"},
                }
            }
        )

    provider = PlotaProvider("secret", opener=opener)
    result = provider.application_by_id("stable-123")

    assert result.application_id == "stable-123"
    assert result.council == "Croydon"
    assert requests == [
        "https://api.plota.co.uk/v1/applications/stable-123?include_contact=false"
    ]


@pytest.mark.parametrize(
    ("failure", "expected_type", "expected_category"),
    [
        (
            HTTPError("https://api.plota.co.uk", 401, "unauthorized", {}, None),
            PlanningAuthenticationError,
            "provider_authentication_error",
        ),
        (
            URLError(TimeoutError("timed out")),
            PlanningTimeoutError,
            "provider_timeout",
        ),
    ],
)
def test_plota_provider_categorises_transport_failures(
    failure, expected_type, expected_category
) -> None:
    def opener(request, timeout):
        raise failure

    provider = PlotaProvider("secret", opener=opener, sleep=lambda seconds: None)
    with pytest.raises(expected_type) as captured:
        provider.application_by_id("stable-123")
    assert captured.value.error_category == expected_category


def test_plota_provider_categorises_malformed_application_response() -> None:
    provider = PlotaProvider(
        "secret", opener=lambda request, timeout: FakeResponse({"data": []})
    )
    with pytest.raises(PlanningMalformedResponseError) as captured:
        provider.application_by_id("stable-123")
    assert captured.value.error_category == "malformed_provider_response"


def test_plota_provider_associated_lookup_retains_only_exact_reference() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(
            {
                "data": {
                    "id": "origin",
                    "reference": "24/03385/FUL",
                    "role": "principal",
                    "principal": {
                        "id": "origin",
                        "reference": "24/03385/FUL",
                        "description": "children's home",
                    },
                    "applications": [
                        {
                            "id": "origin",
                            "reference": "24/03385/FUL",
                            "description": "children's home",
                        },
                        {
                            "id": "other",
                            "reference": "23/02622/FUL",
                            "description": "other application",
                        },
                    ],
                    "conditions": [],
                    "count": 2,
                },
                "meta": {"historical_available": True},
            }
        )

    provider = PlotaProvider("secret", opener=opener)
    search = provider.associated_applications_by_reference(
        "6v5f0ggb", "24/03385/FUL", limit=10
    )

    assert [item.application_id for item in search.candidates] == ["origin"]
    assert search.returned_count == 2
    assert search.truncated is False
    assert requests == ["https://api.plota.co.uk/v1/applications/6v5f0ggb/associated?limit=10"]


def test_plota_provider_associated_lookup_rejects_standard_page_shape() -> None:
    provider = PlotaProvider(
        "secret",
        opener=lambda request, timeout: FakeResponse({"data": []}),
    )

    with pytest.raises(Exception, match="associated response has an invalid shape"):
        provider.associated_applications_by_reference("6v5f0ggb", "24/03385/FUL")


def test_targeted_origin_recovery_uses_normal_ingestion_queue(monkeypatch) -> None:
    results = []
    queued = []

    class ExactProvider:
        def applications_by_reference(self, reference, *, limit):
            assert (reference, limit) == ("24/03385/FUL", 10)
            return PlanningReferenceSearchResult(
                reference,
                (record("Change of use from dwelling to a children's care home"),),
                1,
                False,
            )

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: ExactProvider())
    monkeypatch.setattr(
        "app.collector.send_planning_origin_recovery_result",
        lambda settings, message: results.append(message),
    )
    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    result = recover_planning_origin(
        Settings(
            planning_provider_secret_arn="arn:example",
            ingestion_queue_url="https://sqs.example/ingestion",
            enrichment_queue_url="https://sqs.example/enrichment",
        ),
        {
            "attempt_id": "attempt-1",
            "normalized_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
        },
    )
    assert result == {"requests": 1, "records_fetched": 1, "signals_queued": 1}
    assert [message.status for message in results] == ["FOUND"]
    assert queued[0].signal["vertical"] == "CHILDRENS_HOME"


def test_planning_watch_material_change_uses_normal_ingestion(monkeypatch) -> None:
    results = []
    queued = []

    class ExactProvider:
        requests_made = 1

        def applications_by_reference(self, reference, *, limit):
            return PlanningReferenceSearchResult(
                reference,
                (record("Change of use to a children's home"),),
                1,
                False,
            )

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: ExactProvider())
    monkeypatch.setattr(
        "app.collector.send_planning_lifecycle_watch_result",
        lambda settings, message: results.append(message),
    )
    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    result = refresh_planning_lifecycle_watch(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "run_id": "run-1",
            "watch_id": "watch-1",
            "planning_reference": "24/03385/FUL",
            "planning_authority": "Bristol",
            "latest_snapshot": {},
            "site_postcode": "BS1 1AA",
        },
    )
    assert result == {"requests": 1, "changed": 1, "unchanged": 0, "failed": 0}
    assert results[0].status == "CHANGED"
    assert len(queued) == 1


def test_planning_watch_prefers_stable_provider_id_over_reference_search(monkeypatch) -> None:
    results = []

    class DirectProvider:
        requests_made = 1

        def application_by_id(self, application_id):
            assert application_id == "stable-123"
            return replace(
                record("Change of use to a children's home"),
                application_id=application_id,
                council="London Borough of Croydon",
                raw={"id": application_id, "reference": "24/03385/FUL"},
            )

        def applications_by_reference(self, reference, *, limit):
            raise AssertionError("stable-id watcher must not perform a broad search")

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: DirectProvider())
    monkeypatch.setattr("app.collector.planning_watch_snapshot", lambda metadata: {"same": True})
    monkeypatch.setattr(
        "app.collector.send_planning_lifecycle_watch_result",
        lambda settings, message: results.append(message),
    )
    result = refresh_planning_lifecycle_watch(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "run_id": "run-direct",
            "watch_id": "watch-direct",
            "planning_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
            "provider_application_id": "stable-123",
            "latest_snapshot": {"same": True},
        },
    )

    assert result == {"requests": 1, "changed": 0, "unchanged": 1, "failed": 0}
    assert results[0].details["lookup_method"] == "stable_provider_id"
    assert results[0].details["provider_result"] == "FOUND"


def test_planning_watch_persists_resolution_failure_category(monkeypatch) -> None:
    results = []

    class EmptyProvider:
        requests_made = 1

        def applications_by_reference(self, reference, *, limit):
            return PlanningReferenceSearchResult(reference, (), 0, False)

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: EmptyProvider())
    monkeypatch.setattr(
        "app.collector.send_planning_lifecycle_watch_result",
        lambda settings, message: results.append(message),
    )

    result = refresh_planning_lifecycle_watch(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "run_id": "run-missing",
            "watch_id": "watch-missing",
            "planning_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
        },
    )

    assert result == {"requests": 1, "changed": 0, "unchanged": 0, "failed": 1}
    assert results[0].details["provider_result"] == "NOT_FOUND"
    assert results[0].details["error_category"] == "provider_no_reference_match"


def test_planning_watch_falls_back_to_exact_reference_when_historical_id_is_unknown(
    monkeypatch,
) -> None:
    results = []

    class HistoricalProvider:
        requests_made = 0

        def application_by_id(self, application_id):
            self.requests_made += 1
            raise PlanningProviderError("not found", http_status=404)

        def applications_by_reference(self, reference, *, limit):
            self.requests_made += 1
            return PlanningReferenceSearchResult(reference, (record("Same"),), 1, False)

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr(
        "app.collector.PlotaProvider", lambda *args, **kwargs: HistoricalProvider()
    )
    monkeypatch.setattr("app.collector.planning_watch_snapshot", lambda metadata: {"same": True})
    monkeypatch.setattr(
        "app.collector.send_planning_lifecycle_watch_result",
        lambda settings, message: results.append(message),
    )

    result = refresh_planning_lifecycle_watch(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "run_id": "run-historical-id",
            "watch_id": "watch-historical-id",
            "planning_reference": "24/03385/FUL",
            "planning_authority": "Bristol",
            "provider_application_id": "historical-reference-shaped-id",
            "latest_snapshot": {"same": True},
        },
    )

    assert result == {"requests": 2, "changed": 0, "unchanged": 1, "failed": 0}
    assert results[0].details["lookup_method"] == (
        "stable_provider_id_then_exact_reference"
    )
    assert results[0].details["direct_lookup_http_status"] == 404


def test_planning_watch_unchanged_response_does_not_duplicate_evidence(monkeypatch) -> None:
    results = []
    queued = []

    class ExactProvider:
        requests_made = 1

        def applications_by_reference(self, reference, *, limit):
            return PlanningReferenceSearchResult(reference, (record("Same"),), 1, False)

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: ExactProvider())
    monkeypatch.setattr("app.collector.planning_watch_snapshot", lambda metadata: {"same": True})
    monkeypatch.setattr(
        "app.collector.send_planning_lifecycle_watch_result",
        lambda settings, message: results.append(message),
    )
    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    result = refresh_planning_lifecycle_watch(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "run_id": "run-2",
            "watch_id": "watch-2",
            "planning_reference": "24/03385/FUL",
            "planning_authority": "Bristol",
            "latest_snapshot": {"same": True},
        },
    )
    assert result == {"requests": 1, "changed": 0, "unchanged": 1, "failed": 0}
    assert results[0].status == "UNCHANGED"
    assert queued == []


def test_targeted_origin_recovery_cools_down_not_found(monkeypatch) -> None:
    results = []

    class EmptyProvider:
        def applications_by_reference(self, reference, *, limit):
            return PlanningReferenceSearchResult(reference, (), 0, False)

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr("app.collector.PlotaProvider", lambda *args, **kwargs: EmptyProvider())
    monkeypatch.setattr(
        "app.collector.send_planning_origin_recovery_result",
        lambda settings, message: results.append(message),
    )
    result = recover_planning_origin(
        Settings(
            planning_provider_secret_arn="arn:example",
            enrichment_queue_url="https://sqs.example/enrichment",
        ),
        {
            "attempt_id": "attempt-2",
            "normalized_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
        },
    )
    assert result == {"requests": 1, "records_fetched": 0, "signals_queued": 0}
    assert [message.status for message in results] == ["NOT_FOUND"]


def test_targeted_origin_recovery_selects_ashburton_from_multi_council_results(
    monkeypatch,
) -> None:
    results = []
    queued = []

    def origin(application_id, council, address, postcode):
        return replace(
            record(
                "Alterations and change of use to a children's care home for up to 5 children",
                address=address,
            ),
            application_id=application_id,
            council=council,
            postcode=postcode,
            raw={"id": application_id, "reference": "24/03385/FUL"},
        )

    class MultiCouncilProvider:
        def applications_by_reference(self, reference, *, limit):
            assert reference == "24/03385/FUL"
            return PlanningReferenceSearchResult(
                reference,
                (
                    origin("sheffield", "Sheffield", "1 Other Road", "S1 1AA"),
                    origin("enfield", "Enfield", "2 Other Road", "EN1 1AA"),
                    origin(
                        "croydon",
                        "London Borough of Croydon",
                        "58 Ashburton Road Croydon CR0 6AN",
                        "CR0 6AN",
                    ),
                ),
                3,
                False,
            )

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr(
        "app.collector.PlotaProvider", lambda *args, **kwargs: MultiCouncilProvider()
    )
    monkeypatch.setattr(
        "app.collector.send_planning_origin_recovery_result",
        lambda settings, message: results.append(message),
    )
    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )

    result = recover_planning_origin(
        Settings(
            planning_provider_secret_arn="arn:example",
            ingestion_queue_url="https://sqs.example/ingestion",
            enrichment_queue_url="https://sqs.example/enrichment",
        ),
        {
            "attempt_id": "attempt-ashburton",
            "normalized_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
            "site_address": "58 Ashburton Road Croydon CR0 6AN",
            "site_postcode": "CR0 6AN",
        },
    )

    assert result == {"requests": 1, "records_fetched": 3, "signals_queued": 1}
    assert queued[0].signal["external_id"] == "plota:croydon"
    assert results[0].status == "FOUND"
    assert results[0].details["selected_candidate_id"] == "croydon"
    assert results[0].details["selection_reason"] == "unique_normalized_authority_match"


def test_targeted_origin_recovery_uses_associated_fallback_when_q_has_no_rows(
    monkeypatch,
) -> None:
    results = []
    queued = []

    class AssociatedProvider:
        def applications_by_reference(self, reference, *, limit):
            return PlanningReferenceSearchResult(reference, (), 0, False)

        def associated_applications_by_reference(self, application_id, reference, *, limit):
            assert (application_id, reference, limit) == ("6v5f0ggb", "24/03385/FUL", 10)
            item = replace(
                record(
                    "Alterations and change of use to a children's care home",
                    address="58 Ashburton Road Croydon CR0 6AN",
                ),
                application_id="origin-croydon",
                council="Croydon",
                postcode="CR0 6AN",
                raw={"id": "origin-croydon", "reference": "24/03385/FUL"},
            )
            return PlanningReferenceSearchResult(
                "associated:6v5f0ggb:24/03385/FUL", (item,), 1, False
            )

    monkeypatch.setattr("app.collector.provider_api_key_from_secret", lambda arn: "secret")
    monkeypatch.setattr(
        "app.collector.PlotaProvider", lambda *args, **kwargs: AssociatedProvider()
    )
    monkeypatch.setattr(
        "app.collector.send_planning_origin_recovery_result",
        lambda settings, message: results.append(message),
    )
    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )

    result = recover_planning_origin(
        Settings(planning_provider_secret_arn="arn:example"),
        {
            "attempt_id": "attempt-associated",
            "normalized_reference": "24/03385/FUL",
            "planning_authority": "Croydon",
            "triggering_provider_id": "6v5f0ggb",
            "site_postcode": "CR0 6AN",
        },
    )

    assert result == {"requests": 2, "records_fetched": 1, "signals_queued": 1}
    assert queued[0].signal["external_id"] == "plota:origin-croydon"
    assert results[0].status == "FOUND"
    assert results[0].details["provider_queries"] == [
        "24/03385/FUL",
        "associated:6v5f0ggb:24/03385/FUL",
    ]


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
        "nursery_matched": 1,
        "care_matched": 0,
    }
    assert queued[0].signal["external_id"] == "plota:abc123"
    assert logged == [
        "planning_collection_summary fetched=2 matched=1 queued=1 duplicates=0 "
        "excluded=1 errors=0 nursery_matched=1 care_matched=0 "
        "lookback_days=2 max_records=100 care_max_records=50 page_size=50 source=manual"
    ]


def test_shared_planning_collector_honours_requested_vertical(monkeypatch) -> None:
    queued = []

    class FakeProvider:
        def applications(self, query):
            yield record("Change of use from dwelling to a children's home for two children")

    monkeypatch.setattr(
        "app.collector.send_ingestion_message", lambda settings, message: queued.append(message)
    )
    settings = Settings(ingestion_queue_url="https://sqs.example/ingestion")
    counts = collect_planning(
        settings,
        {
            "from_date": "2026-09-23",
            "to_date": "2026-09-24",
            "source": "manual",
            "verticals": ["CHILDRENS_HOME"],
        },
        FakeProvider(),
    )
    assert counts["care_matched"] == 1
    assert counts["nursery_matched"] == 0
    assert len(queued) == 1
    assert queued[0].signal["vertical"] == "CHILDRENS_HOME"


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
