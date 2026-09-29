from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.authorization import normalized_groups
from app.handler import handler
from app.repository import ReprocessResult
from app.service import IngestionResult

CLAIMS = {
    "sub": "reviewer-123",
    "username": "admin@example.test",
    "cognito:groups": ["NurserySignalAdmins"],
}


def event(
    path: str,
    method: str = "GET",
    *,
    body: str | None = None,
    query: dict[str, str] | None = None,
    claims: dict | None = None,
) -> dict:
    return {
        "rawPath": path,
        "body": body,
        "queryStringParameters": query or {},
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": claims or CLAIMS}},
        },
    }


def signal_payload(external_id: str = "fixture-1") -> dict:
    return {
        "schema_version": "1.0",
        "source_type": "planning",
        "source_url": "https://example.test/planning/1",
        "external_id": external_id,
        "discovered_at": "2026-09-24T08:00:00Z",
        "title": "New nursery planning application",
        "raw_text": "A new nursery is proposed.",
        "location_hint": "Bristol BS1",
        "organisation_hint": "Little Acorns",
        "metadata": {},
    }


def test_public_access_request_is_unauthenticated_and_bounded(monkeypatch) -> None:
    captured = {}

    def fake_create(settings, payload):
        captured.update(payload)
        return {"status": "accepted", "request_id": "request-1"}

    monkeypatch.setattr("app.handler.create_access_request", fake_create)
    response = handler(
        event(
            "/public/access-requests",
            "POST",
            claims={},
            body=json.dumps(
                {
                    "name": "Alex Supplier",
                    "company": "Example Ltd",
                    "email": "alex@example.test",
                    "supplier_category": "SOFTWARE",
                }
            ),
        ),
        None,
    )
    assert response["statusCode"] == 202
    assert captured["company"] == "Example Ltd"


def test_access_request_list_remains_admin_only(monkeypatch) -> None:
    monkeypatch.setattr("app.handler.list_access_requests", lambda settings: [{"id": "lead-1"}])
    denied = handler(event("/admin/access-requests", claims={"sub": "outsider"}), None)
    assert denied["statusCode"] == 403
    allowed = handler(event("/admin/access-requests"), None)
    assert allowed["statusCode"] == 200


def test_signal_requires_authentication() -> None:
    response = handler(
        {"rawPath": "/signals", "requestContext": {"http": {"method": "POST"}}}, None
    )
    assert response["statusCode"] == 401


def test_invalid_signal_is_rejected_without_storage() -> None:
    response = handler(
        event("/signals", "POST", body=json.dumps({"source_type": "planning"})), None
    )
    assert response["statusCode"] == 400


def test_successful_ingestion_returns_signal_id(monkeypatch) -> None:
    result = IngestionResult(
        "b3b7c6bd-6079-4bd6-9b42-c9e6e80d1e4d", "accepted", "signals/x/raw.json", True
    )
    monkeypatch.setattr("app.handler.ingest_signal", lambda settings, signal, body: result)
    response = handler(event("/signals", "POST", body=json.dumps(signal_payload())), None)
    assert response["statusCode"] == 201
    assert json.loads(response["body"])["signal_id"] == result.signal_id


def test_duplicate_ingestion_is_idempotent(monkeypatch) -> None:
    result = IngestionResult(
        "b3b7c6bd-6079-4bd6-9b42-c9e6e80d1e4d", "duplicate", "signals/x/raw.json", True
    )
    monkeypatch.setattr("app.handler.ingest_signal", lambda settings, signal, body: result)
    response = handler(event("/signals", "POST", body=json.dumps(signal_payload())), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "duplicate"


def test_database_failure_is_not_reported_as_success(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.ingest_signal", lambda *args: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    response = handler(event("/signals", "POST", body=json.dumps(signal_payload())), None)
    assert response["statusCode"] == 500


def test_admin_list_filters_and_paginates(monkeypatch) -> None:
    captured = {}

    def fake_list(settings, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0, **kwargs}

    monkeypatch.setattr("app.handler.list_signals", fake_list)
    response = handler(
        event(
            "/admin/signals",
            query={
                "limit": "10",
                "offset": "20",
                "review_status": "PENDING",
                "source_type": "planning",
            },
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured == {
        "limit": 10,
        "offset": 20,
        "review_status": "PENDING",
        "source_type": "planning",
        "discovered_from": None,
        "discovered_to": None,
        "search": None,
        "unmatched_only": False,
        "include_excluded": False,
        "opportunity_decision": None,
        "vertical": "NURSERY",
        "triage_bucket": None,
        "planning_subtype": None,
    }


def test_bulk_review_requires_admin_and_enforces_bounded_ids(monkeypatch) -> None:
    signal_ids = [str(uuid4()), str(uuid4())]
    captured = {}

    def fake_bulk(settings, values, status, reviewer):
        captured.update(values=values, status=status, reviewer=reviewer)
        return {"updated": 2, "skipped": 0}

    monkeypatch.setattr("app.handler.review_signals_bulk", fake_bulk)
    response = handler(
        event(
            "/admin/signals/bulk-review",
            "POST",
            body=json.dumps({"action": "approve", "signal_ids": signal_ids}),
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured == {"values": signal_ids, "status": "APPROVED", "reviewer": "staff"}

    denied = handler(
        event(
            "/admin/signals/bulk-review",
            "POST",
            body=json.dumps({"action": "approve", "signal_ids": signal_ids}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403

    too_many = handler(
        event(
            "/admin/signals/bulk-review",
            "POST",
            body=json.dumps({"action": "reject", "signal_ids": [str(uuid4())] * 101}),
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert too_many["statusCode"] == 400


def test_admin_list_passes_bounded_text_search(monkeypatch) -> None:
    captured = {}

    def fake_list(settings, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0, **kwargs}

    monkeypatch.setattr("app.handler.list_signals", fake_list)
    response = handler(event("/admin/signals", query={"q": "nursery planning ref"}), None)
    assert response["statusCode"] == 200
    assert captured["search"] == "nursery planning ref"


def test_admin_list_validates_and_forwards_triage_bucket(monkeypatch) -> None:
    captured = {}

    def fake_list(settings, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    monkeypatch.setattr("app.handler.list_signals", fake_list)
    response = handler(
        event(
            "/admin/signals",
            query={"triage_bucket": "SAFE_APPROVE_AGREEMENT"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["triage_bucket"] == "SAFE_APPROVE_AGREEMENT"
    invalid = handler(
        event("/admin/signals", query={"triage_bucket": "arbitrary_expression"}),
        None,
    )
    assert invalid["statusCode"] == 400


def test_admin_list_validates_and_forwards_planning_subtype(monkeypatch) -> None:
    captured = {}

    def fake_list(settings, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    monkeypatch.setattr("app.handler.list_signals", fake_list)
    response = handler(
        event(
            "/admin/signals",
            query={"planning_subtype": "CONDITION_VARIATION"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["planning_subtype"] == "CONDITION_VARIATION"
    invalid = handler(
        event("/admin/signals", query={"planning_subtype": "arbitrary_expression"}),
        None,
    )
    assert invalid["statusCode"] == 400


def test_admin_lists_validate_and_forward_vertical_scope(monkeypatch) -> None:
    captured = {}

    def fake_list(settings, **kwargs):
        captured.update(kwargs)
        return {"items": [], "total": 0}

    monkeypatch.setattr("app.handler.list_signals", fake_list)
    response = handler(event("/admin/signals", query={"vertical": "CHILDRENS_HOME"}), None)
    assert response["statusCode"] == 200
    assert captured["vertical"] == "CHILDRENS_HOME"

    response = handler(event("/admin/signals", query={"vertical": "ALL"}), None)
    assert response["statusCode"] == 200
    assert captured["vertical"] == "ALL"

    assert (
        handler(event("/admin/signals", query={"vertical": "UNKNOWN"}), None)["statusCode"] == 400
    )
    assert handler(event("/admin/signals", query={"vertical": "DENTAL"}), None)["statusCode"] == 400


def test_admin_work_queues_forward_same_validated_vertical(monkeypatch) -> None:
    admin_claims = {**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]}
    captured = {}

    monkeypatch.setattr(
        "app.handler.list_opportunities",
        lambda settings, **kwargs: (
            captured.setdefault("opportunities", kwargs) or {"items": [], "total": 0}
        ),
    )
    monkeypatch.setattr(
        "app.handler.list_match_reviews",
        lambda settings, **kwargs: (
            captured.setdefault("match_review", kwargs) or {"items": [], "total": 0}
        ),
    )
    monkeypatch.setattr(
        "app.handler.list_organisations",
        lambda settings, **kwargs: captured.setdefault("organisations", kwargs) or {"items": []},
    )

    for path, key in (
        ("/admin/opportunities", "opportunities"),
        ("/admin/match-review", "match_review"),
        ("/admin/organisations", "organisations"),
    ):
        response = handler(
            event(path, query={"vertical": "CHILDRENS_HOME"}, claims=admin_claims),
            None,
        )
        assert response["statusCode"] == 200
        assert captured[key]["vertical"] == "CHILDRENS_HOME"


def test_opportunity_recalculate_requires_admin_and_is_bounded(monkeypatch) -> None:
    captured = {}

    def fake_recalculate(settings, **kwargs):
        captured.update(kwargs)
        return {"selected": 1}

    monkeypatch.setattr("app.handler.recalculate_opportunity_creation", fake_recalculate)
    signal_id = str(uuid4())
    response = handler(
        event(
            "/admin/opportunities/recalculate",
            "POST",
            body=json.dumps({"limit": 999, "signal_ids": [signal_id]}),
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured == {
        "actor": "staff",
        "limit": 25,
        "offset": 0,
        "signal_ids": [signal_id],
        "vertical": "NURSERY",
    }

    denied = handler(
        event(
            "/admin/opportunities/recalculate",
            "POST",
            body=json.dumps({"limit": 1}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_care_backfill_is_admin_only_and_bounded(monkeypatch) -> None:
    captured = {}

    def fake_backfill(settings, **kwargs):
        captured.update(kwargs)
        return {"evaluated": 0, "relevant": 0}

    monkeypatch.setattr("app.handler.backfill_care_from_stored_evidence", fake_backfill)
    response = handler(
        event(
            "/admin/verticals/CHILDRENS_HOME/backfill",
            "POST",
            body=json.dumps({"days": 999, "limit": 999}),
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured == {"actor": "staff", "days": 90, "limit": 50}

    denied = handler(
        event(
            "/admin/verticals/CHILDRENS_HOME/backfill",
            "POST",
            body=json.dumps({}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_historical_backtest_is_admin_only_and_bounded(monkeypatch) -> None:
    captured = {}

    def fake_execute(settings, **kwargs):
        captured.update(kwargs)
        return {"id": "run-1", "status": "SUCCESS", "metrics": {}}

    monkeypatch.setattr("app.handler.execute_backtest", fake_execute)
    response = handler(
        event(
            "/admin/backtesting/run",
            "POST",
            body=json.dumps(
                {
                    "vertical": "CHILDRENS_HOME",
                    "benchmark_version": "care-ofsted-v1",
                    "as_of": "2026-09-27T23:59:59Z",
                    "lookback_days": 999,
                    "max_cases": 999,
                    "max_signals": 9999,
                }
            ),
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["vertical"] == "CHILDRENS_HOME"
    assert captured["bounds"].lookback_days == 365
    assert captured["bounds"].max_cases == 50
    assert captured["bounds"].max_signals == 1000

    denied = handler(
        event(
            "/admin/backtesting/run",
            "POST",
            body=json.dumps({"vertical": "CHILDRENS_HOME", "as_of": "2026-09-27"}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_backtest_sensitivity_and_recruitment_preview_are_admin_only(monkeypatch) -> None:
    sensitivity = {}

    def fake_sensitivity(settings, **kwargs):
        sensitivity.update(kwargs)
        return {"runs": [], "read_only": True}

    monkeypatch.setattr("app.handler.execute_backtest_sensitivity", fake_sensitivity)
    monkeypatch.setattr(
        "app.handler.evaluate_current_care_recruitment",
        lambda settings, **kwargs: {
            "evaluated": kwargs["limit"],
            "counts": {},
            "changed_count": 0,
            "changed": [],
        },
    )
    allowed_claims = {"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]}
    response = handler(
        event(
            "/admin/backtesting/sensitivity",
            "POST",
            body=json.dumps({"as_of": "2026-09-27", "max_cases": 999, "max_signals": 9999}),
            claims=allowed_claims,
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert sensitivity["vertical"] == "CHILDRENS_HOME"
    assert sensitivity["max_cases"] == 50
    assert sensitivity["max_signals"] == 1000

    preview = handler(
        event(
            "/admin/backtesting/recruitment-shadow",
            "GET",
            query={"limit": "999"},
            claims=allowed_claims,
        ),
        None,
    )
    assert preview["statusCode"] == 200
    assert json.loads(preview["body"])["evaluated"] == 250

    denied = handler(
        event(
            "/admin/backtesting/sensitivity",
            "POST",
            body="{}",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_historical_corpus_import_is_admin_only_and_uses_bundled_manifest(monkeypatch) -> None:
    captured = {}

    def fake_import(settings, **kwargs):
        captured.update(kwargs)
        return {"corpus_version": "care-historical-research-v1", "idempotent": False}

    monkeypatch.setattr("app.handler.import_bundled_historical_corpus", fake_import)
    response = handler(
        event(
            "/admin/backtesting/research/import",
            "POST",
            body="{}",
            claims={"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured == {
        "actor": "staff",
        "manifest_name": "care_historical_research_v2.json",
    }

    denied = handler(
        event(
            "/admin/backtesting/research/import",
            "POST",
            body="{}",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_backtest_rejects_all_verticals_and_invalid_dates(monkeypatch) -> None:
    admin = {"sub": "staff", "cognito:groups": ["NurserySignalAdmins"]}
    all_verticals = handler(
        event(
            "/admin/backtesting/run",
            "POST",
            body=json.dumps({"vertical": "ALL", "as_of": "2026-09-27"}),
            claims=admin,
        ),
        None,
    )
    assert all_verticals["statusCode"] == 400
    invalid_date = handler(
        event(
            "/admin/backtesting/run",
            "POST",
            body=json.dumps({"vertical": "CHILDRENS_HOME", "as_of": "not-a-date"}),
            claims=admin,
        ),
        None,
    )
    assert invalid_date["statusCode"] == 400


def test_sources_status_is_admin_only_and_includes_recent_runs(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.list_runs",
        lambda settings, source_key, limit=10: [
            {
                "id": "run-1",
                "status": "SUCCESS",
                "started_at": "2026-09-26T18:00:00Z",
                "completed_at": "2026-09-26T18:00:02Z",
                "invocation_source": "scheduled",
                "counts": {"records_fetched": 4},
                "parameters": {},
            }
        ],
    )
    admin_claims = {**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]}
    response = handler(event("/admin/sources", claims=admin_claims), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert {item["key"] for item in body["items"]} == {
        "planning",
        "recruitment",
        "companies_house",
    }
    assert body["items"][0]["last_run"]["status"] == "SUCCESS"
    assert (
        handler(
            event("/admin/sources", claims={"sub": "staff", "cognito:groups": ["Other"]}),
            None,
        )["statusCode"]
        == 403
    )


def test_care_sources_include_manual_ofsted_and_companies_house(monkeypatch) -> None:
    monkeypatch.setattr("app.handler.list_runs", lambda *args, **kwargs: [])
    response = handler(
        event(
            "/admin/sources",
            query={"vertical": "CHILDRENS_HOME"},
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    items = {item["key"]: item for item in json.loads(response["body"])["items"]}
    assert set(items) == {
        "planning",
        "recruitment",
        "ofsted",
        "companies_house",
        "procurement",
    }
    assert items["ofsted"]["schedule_state"] == "DISABLED"
    assert items["companies_house"]["schedule_expression"] == "Manual only"
    assert items["procurement"]["schedule_state"] == "DISABLED"


def test_procurement_evaluation_is_admin_only_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.list_procurement_evaluations",
        lambda settings, limit, offset: {"items": [{"id": "p-1"}], "total": 1},
    )
    assert (
        handler(
            event(
                "/admin/procurement-evaluation",
                claims={"sub": "staff", "cognito:groups": ["Other"]},
            ),
            None,
        )["statusCode"]
        == 403
    )
    response = handler(
        event(
            "/admin/procurement-evaluation",
            query={"vertical": "CHILDRENS_HOME", "limit": "9999"},
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["total"] == 1


def test_source_manual_run_uses_fixed_bounds_and_exact_lambda(monkeypatch) -> None:
    settings = SimpleNamespace(
        environment="test",
        admin_group="NurserySignalAdmins",
        source_runs_table_name=None,
        planning_manual_run_queue_url="https://sqs.example/planning-manual-runs",
        recruitment_manual_run_queue_url="https://sqs.example/recruitment-manual-runs",
    )
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "app.handler.start_run", lambda *args, **kwargs: ("run-1", "2026-09-26T19:00:00Z")
    )
    monkeypatch.setattr("app.handler.record_admin_audit", lambda *args, **kwargs: "audit-1")
    calls = []

    class FakeSqs:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "message-1"}

    monkeypatch.setattr("app.handler.boto3.client", lambda name: FakeSqs())
    response = handler(
        event(
            "/admin/sources/planning/run",
            "POST",
            body=json.dumps({"max_records": 999999, "function_name": "evil"}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 202
    assert calls[0]["QueueUrl"] == settings.planning_manual_run_queue_url
    payload = json.loads(calls[0]["MessageBody"])
    assert payload == {
        "source": "manual",
        "lookback_days": 2,
        "max_records": 100,
        "care_max_records": 50,
        "page_size": 25,
        "verticals": ["NURSERY"],
        "run_id": "run-1",
        "run_started_at": "2026-09-26T19:00:00Z",
    }


def test_historical_planning_backfill_queues_only_first_bounded_chunk(monkeypatch) -> None:
    settings = SimpleNamespace(
        environment="test",
        admin_group="NurserySignalAdmins",
        source_runs_table_name=None,
        planning_manual_run_queue_url="https://sqs.example/planning-manual-runs",
    )
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "app.handler.start_run",
        lambda *args, **kwargs: ("backfill-1", "2026-09-29T12:00:00+00:00"),
    )
    monkeypatch.setattr("app.handler.record_admin_audit", lambda *args, **kwargs: "audit-1")
    sent = []

    class FakeSqs:
        def send_message(self, **kwargs):
            sent.append(json.loads(kwargs["MessageBody"]))
            return {"MessageId": "message-1"}

    monkeypatch.setattr("app.handler.boto3.client", lambda name: FakeSqs())
    response = handler(
        event(
            "/admin/sources/planning/backfill",
            "POST",
            body=json.dumps(
                {
                    "from_date": "2025-03-29",
                    "to_date": "2026-09-29",
                    "vertical": "ALL",
                    "max_records": 60000,
                }
            ),
        ),
        None,
    )
    assert response["statusCode"] == 202
    assert len(sent) == 1
    assert sent[0]["chunk_index"] == 0
    assert sent[0]["chunks_total"] == 79
    assert sent[0]["from_date"] == "2025-03-29"
    assert sent[0]["to_date"] == "2025-04-04"
    assert sent[0]["verticals"] == ["NURSERY", "CHILDRENS_HOME"]


def test_companies_house_manual_run_uses_server_selected_bounded_candidates(
    monkeypatch,
) -> None:
    settings = SimpleNamespace(
        environment="test",
        admin_group="NurserySignalAdmins",
        source_runs_table_name=None,
        planning_manual_run_queue_url=None,
        recruitment_manual_run_queue_url=None,
        ofsted_manual_run_queue_url=None,
        companies_house_manual_run_queue_url="https://sqs.example/companies-house",
    )
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "app.handler.list_organisation_enrichment_candidates",
        lambda settings, limit, vertical: [
            {"operator_id": "op-1", "name": "Acme Care Limited", "locality": "Coventry"}
        ],
    )
    monkeypatch.setattr(
        "app.handler.start_run", lambda *args, **kwargs: ("run-1", "2026-09-27T05:00:00Z")
    )
    monkeypatch.setattr("app.handler.record_admin_audit", lambda *args, **kwargs: "audit-1")
    sent = []

    class FakeSqs:
        def send_message(self, **kwargs):
            sent.append(json.loads(kwargs["MessageBody"]))
            return {"MessageId": "message-1"}

    monkeypatch.setattr("app.handler.boto3.client", lambda name: FakeSqs())
    response = handler(
        event(
            "/admin/sources/companies_house/run",
            "POST",
            body=json.dumps({"vertical": "CHILDRENS_HOME", "max_organisations": 999}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 202
    assert sent[0]["max_organisations"] == 10
    assert sent[0]["organisation_candidates"][0]["operator_id"] == "op-1"


def test_admin_detail_and_review_actions(monkeypatch) -> None:
    signal_id = str(uuid4())
    monkeypatch.setattr("app.handler.signal_detail", lambda settings, value: {"id": UUID(value)})
    monkeypatch.setattr(
        "app.handler.review_signal", lambda settings, value, status, reviewer: status == "APPROVED"
    )
    assert handler(event(f"/admin/signals/{signal_id}"), None)["statusCode"] == 200
    approved = handler(event(f"/admin/signals/{signal_id}/approve", "POST"), None)
    rejected = handler(event(f"/admin/signals/{signal_id}/reject", "POST"), None)
    assert approved["statusCode"] == 200
    assert rejected["statusCode"] == 404


def test_admin_evidence_url_is_authenticated_and_short_lived(monkeypatch) -> None:
    signal_id = str(uuid4())
    monkeypatch.setattr(
        "app.handler.signal_detail",
        lambda settings, value: {
            "documents": [{"s3_bucket": "private", "s3_key": "signals/raw.json"}]
        },
    )
    monkeypatch.setattr(
        "app.handler.presigned_evidence_url",
        lambda bucket, key: f"https://signed.test/{bucket}/{key}",
    )
    response = handler(event(f"/admin/signals/{signal_id}/evidence"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "url": "https://signed.test/private/signals/raw.json",
        "expires_in": 300,
    }


def test_admin_detail_serializes_database_numeric_values(monkeypatch) -> None:
    signal_id = str(uuid4())
    monkeypatch.setattr(
        "app.handler.signal_detail",
        lambda settings, value: {"id": UUID(value), "confidence": Decimal("0.86")},
    )
    response = handler(event(f"/admin/signals/{signal_id}"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["confidence"] == 0.86


def test_planning_reprocess_requires_administrator_group() -> None:
    response = handler(
        event(
            "/admin/planning/reprocess",
            "POST",
            body=json.dumps({"limit": 25}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "administrator_role_required"}


def test_normalized_groups_accepts_list() -> None:
    assert normalized_groups(["NurserySignalAdmins", "OtherGroup"]) == {
        "NurserySignalAdmins",
        "OtherGroup",
    }


def test_normalized_groups_accepts_exact_single_string() -> None:
    assert normalized_groups("NurserySignalAdmins") == {"NurserySignalAdmins"}


def test_normalized_groups_accepts_comma_separated_string() -> None:
    assert normalized_groups("OtherGroup, NurserySignalAdmins") == {
        "OtherGroup",
        "NurserySignalAdmins",
    }


def test_normalized_groups_accepts_json_array_string() -> None:
    assert normalized_groups('["OtherGroup", "NurserySignalAdmins"]') == {
        "OtherGroup",
        "NurserySignalAdmins",
    }


def test_normalized_groups_accepts_api_gateway_bracketed_string() -> None:
    assert normalized_groups("[NurserySignalAdmins]") == {"NurserySignalAdmins"}


def test_normalized_groups_rejects_missing_and_malformed_claims() -> None:
    assert normalized_groups(None) == set()
    assert normalized_groups({"group": "NurserySignalAdmins"}) == set()
    assert normalized_groups(42) == set()


def test_normalized_groups_requires_exact_group_name() -> None:
    assert "NurserySignalAdmins" not in normalized_groups("NurserySignalAdministrators")


def test_reprocess_accepts_json_serialized_admin_groups(monkeypatch) -> None:
    expected = ReprocessResult("operation-1", 0, 0, 0, 0, 0, 0)
    monkeypatch.setattr("app.handler.reprocess_planning_signals", lambda *args, **kwargs: expected)
    response = handler(
        event(
            "/admin/planning/reprocess",
            "POST",
            body=json.dumps({"limit": 1}),
            claims={**CLAIMS, "cognito:groups": '["NurserySignalAdmins"]'},
        ),
        None,
    )
    assert response["statusCode"] == 200


def test_reprocess_rejects_similar_group_name() -> None:
    response = handler(
        event(
            "/admin/planning/reprocess",
            "POST",
            body=json.dumps({"limit": 1}),
            claims={**CLAIMS, "cognito:groups": "NurserySignalAdministrators"},
        ),
        None,
    )
    assert response["statusCode"] == 403


def test_planning_reprocess_passes_bounded_filters_and_returns_audit_summary(monkeypatch) -> None:
    captured = {}
    expected = ReprocessResult("operation-1", 2, 1, 1, 0, 1, 1)

    def fake_reprocess(settings, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr("app.handler.reprocess_planning_signals", fake_reprocess)
    response = handler(
        event(
            "/admin/planning/reprocess",
            "POST",
            body=json.dumps(
                {
                    "limit": 500,
                    "discovered_from": "2026-09-01",
                    "discovered_to": "2026-09-24",
                    "signal_ids": [str(uuid4())],
                }
            ),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["limit"] == 100
    assert captured["discovered_from"] == "2026-09-01"
    assert captured["discovered_to"] == "2026-09-24"
    assert len(captured["signal_ids"]) == 1
    assert json.loads(response["body"])["reviewed_preserved"] == 1


def test_planning_reprocess_rejects_unbounded_id_lists() -> None:
    response = handler(
        event(
            "/admin/planning/reprocess",
            "POST",
            body=json.dumps({"limit": 1, "signal_ids": [str(uuid4()), str(uuid4())]}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 400


def test_recruitment_reprocess_is_admin_only_and_bounded(monkeypatch) -> None:
    expected = {
        "operation_id": "operation-2",
        "selected": 3,
        "pending_updated": 3,
        "reviewed_preserved": 0,
        "matched": 3,
        "excluded": 0,
    }
    captured = {}

    def fake_reprocess(settings, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr("app.handler.reprocess_recruitment_signals", fake_reprocess)
    denied = handler(
        event(
            "/admin/recruitment/reprocess",
            "POST",
            body=json.dumps({"limit": 200}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403
    response = handler(
        event(
            "/admin/recruitment/reprocess",
            "POST",
            body=json.dumps({"limit": 200, "signal_ids": [str(uuid4())]}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["limit"] == 100
    assert len(captured["signal_ids"]) == 1


def test_recruitment_reprocess_rejects_unbounded_id_lists() -> None:
    response = handler(
        event(
            "/admin/recruitment/reprocess",
            "POST",
            body=json.dumps({"limit": 1, "signal_ids": [str(uuid4()), str(uuid4())]}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 400


def test_review_triage_is_admin_only_and_vertical_scoped(monkeypatch) -> None:
    captured = {}

    def fake_summary(settings, **kwargs):
        captured.update(kwargs)
        return {"vertical": kwargs["vertical"], "pending_buckets": {}}

    monkeypatch.setattr("app.handler.review_triage_summary", fake_summary)
    denied = handler(
        event(
            "/admin/review-triage",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403
    response = handler(
        event(
            "/admin/review-triage",
            query={"vertical": "CHILDRENS_HOME"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["vertical"] == "CHILDRENS_HOME"


def test_refusal_cleanup_and_safe_approve_are_bounded_admin_actions(monkeypatch) -> None:
    cleanup_args = {}
    approve_args = {}
    monkeypatch.setattr(
        "app.handler.cleanup_refused_planning_signals",
        lambda settings, **kwargs: cleanup_args.update(kwargs) or {"auto_rejected": 2},
    )
    monkeypatch.setattr(
        "app.handler.safe_agreement_bulk_approve",
        lambda settings, **kwargs: approve_args.update(kwargs) or {"preview": True},
    )
    cleanup = handler(
        event(
            "/admin/review-triage/refusals",
            "POST",
            body=json.dumps({"limit": 99999, "vertical": "CHILDRENS_HOME"}),
        ),
        None,
    )
    preview = handler(
        event(
            "/admin/review-triage/safe-approve",
            "POST",
            body=json.dumps(
                {
                    "preview": True,
                    "limit": 999,
                    "threshold": 0.97,
                    "vertical": "CHILDRENS_HOME",
                }
            ),
        ),
        None,
    )
    assert cleanup["statusCode"] == 200
    assert cleanup_args["limit"] == 2500
    assert cleanup_args["vertical"] == "CHILDRENS_HOME"
    assert preview["statusCode"] == 200
    assert approve_args["limit"] == 100
    assert approve_args["preview"] is True
    assert approve_args["vertical"] == "CHILDRENS_HOME"


def test_care_planning_backlog_actions_are_admin_only_and_bounded(monkeypatch) -> None:
    reclassify_args = {}
    withdrawn_args = {}
    fastpath_args = {}
    ai_approval_args = {}
    monkeypatch.setattr(
        "app.handler.reclassify_pending_care_planning",
        lambda settings, **kwargs: reclassify_args.update(kwargs) or {"updated": 1},
    )
    monkeypatch.setattr(
        "app.handler.cleanup_withdrawn_care_planning_signals",
        lambda settings, **kwargs: withdrawn_args.update(kwargs) or {"auto_rejected": 1},
    )
    monkeypatch.setattr(
        "app.handler.care_planning_fastpath_backlog",
        lambda settings, **kwargs: fastpath_args.update(kwargs) or {"preview": True},
    )
    monkeypatch.setattr(
        "app.handler.care_planning_ai_approval_backlog",
        lambda settings, **kwargs: ai_approval_args.update(kwargs) or {"preview": True},
    )
    reclassified = handler(
        event(
            "/admin/review-triage/care-planning/reclassify",
            "POST",
            body=json.dumps({"limit": 99999, "signal_ids": [str(uuid4())]}),
        ),
        None,
    )
    withdrawn = handler(
        event(
            "/admin/review-triage/care-planning/withdrawn",
            "POST",
            body=json.dumps({"limit": 99999}),
        ),
        None,
    )
    preview = handler(
        event(
            "/admin/review-triage/care-planning/fastpath",
            "POST",
            body=json.dumps({"preview": True, "limit": 999}),
        ),
        None,
    )
    ai_preview = handler(
        event(
            "/admin/review-triage/care-planning/ai-approval",
            "POST",
            body=json.dumps({"preview": True, "limit": 999}),
        ),
        None,
    )
    assert reclassified["statusCode"] == 200 and reclassify_args["limit"] == 2500
    assert len(reclassify_args["signal_ids"]) == 1
    assert withdrawn["statusCode"] == 200 and withdrawn_args["limit"] == 2500
    assert preview["statusCode"] == 200
    assert fastpath_args["preview"] is True and fastpath_args["limit"] == 100
    assert ai_preview["statusCode"] == 200
    assert ai_approval_args["preview"] is True and ai_approval_args["limit"] == 100
    denied = handler(
        event(
            "/admin/review-triage/care-planning/fastpath",
            "POST",
            body=json.dumps({"preview": True}),
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_care_planning_outcome_dry_run_is_admin_only_bounded_and_read_only(
    monkeypatch,
) -> None:
    captured = {}
    monkeypatch.setattr(
        "app.handler.planning_outcome_dry_run",
        lambda settings, **kwargs: captured.update(kwargs)
        or {"read_only": True, "pending_inspected": 12},
    )
    response = handler(
        event(
            "/admin/review-triage/care-planning/outcomes",
            query={"limit": "99999"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["limit"] == 2500
    denied = handler(
        event(
            "/admin/review-triage/care-planning/outcomes",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_care_planning_ai_validation_is_admin_only_bounded_and_non_mutating(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "app.handler.care_planning_ai_validation_preview",
        lambda settings, **kwargs: captured.update(kwargs) or {"policy_preview": {}},
    )
    preview = handler(
        event(
            "/admin/review-triage/care-planning/ai-validation",
            query={"limit": "999"},
        ),
        None,
    )
    assert preview["statusCode"] == 200
    assert captured["sample_limit"] == 100

    evaluated = {}
    monkeypatch.setattr(
        "app.handler.run_care_planning_ai_validation",
        lambda settings, **kwargs: evaluated.update(kwargs) or {"review_decisions_mutated": False},
    )
    response = handler(
        event(
            "/admin/review-triage/care-planning/ai-validation",
            "POST",
            body=json.dumps({"operation": "evaluate", "signal_ids": [str(uuid4())]}),
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert evaluated["actor"] == CLAIMS["sub"]

    refreshed = {}
    monkeypatch.setattr(
        "app.handler.refresh_stale_care_planning_ai",
        lambda settings, **kwargs: refreshed.update(kwargs) or {"review_decisions_mutated": False},
    )
    response = handler(
        event(
            "/admin/review-triage/care-planning/ai-validation",
            "POST",
            body=json.dumps({"operation": "refresh_stale", "limit": 999, "include_missing": True}),
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert refreshed["limit"] == 10
    assert refreshed["include_missing"] is True
    assert refreshed["actor"] == CLAIMS["sub"]

    denied = handler(
        event(
            "/admin/review-triage/care-planning/ai-validation",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403


def test_recruitment_planning_diagnostic_is_bounded_and_read_only(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        "app.handler.recruitment_planning_diagnostic",
        lambda settings, **kwargs: captured.update(kwargs) or {"read_only": True},
    )
    response = handler(
        event(
            "/admin/recruitment/planning-diagnostic",
            query={"limit": "500"},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["limit"] == 50


def test_opportunity_views_and_corrections_are_admin_only(monkeypatch) -> None:
    denied = handler(
        event(
            "/admin/opportunities",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403

    monkeypatch.setattr(
        "app.handler.list_opportunities",
        lambda settings, **kwargs: {"items": [], "total": 0, **kwargs},
    )
    response = handler(
        event("/admin/opportunities", claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]}),
        None,
    )
    assert response["statusCode"] == 200

    captured = {}
    monkeypatch.setattr(
        "app.handler.link_signal_to_opportunity",
        lambda settings, opportunity_id, signal_id, actor, reason: (
            captured.update(
                opportunity_id=opportunity_id, signal_id=signal_id, actor=actor, reason=reason
            )
            or {"status": "ACTIVE"}
        ),
    )
    signal_id = str(uuid4())
    opportunity_id = str(uuid4())
    response = handler(
        event(
            f"/admin/opportunities/{opportunity_id}/link",
            "POST",
            body=json.dumps({"signal_id": signal_id, "reason": "same postcode"}),
            claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]},
        ),
        None,
    )
    assert response["statusCode"] == 200
    assert captured["signal_id"] == signal_id


def test_match_review_is_admin_only_and_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.list_match_reviews",
        lambda settings, **kwargs: {"items": [], "total": 0, **kwargs},
    )
    denied = handler(
        event(
            "/admin/match-review",
            claims={"sub": "staff", "cognito:groups": ["Other"]},
        ),
        None,
    )
    assert denied["statusCode"] == 403
    response = handler(
        event("/admin/match-review", claims={**CLAIMS, "cognito:groups": ["NurserySignalAdmins"]}),
        None,
    )
    assert response["statusCode"] == 200
