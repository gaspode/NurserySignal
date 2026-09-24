from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID, uuid4

from app.handler import handler
from app.service import IngestionResult

CLAIMS = {"sub": "reviewer-123", "username": "admin@example.test"}


def event(
    path: str, method: str = "GET", *, body: str | None = None, query: dict[str, str] | None = None
) -> dict:
    return {
        "rawPath": path,
        "body": body,
        "queryStringParameters": query or {},
        "requestContext": {
            "http": {"method": method},
            "authorizer": {"jwt": {"claims": CLAIMS}},
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
    }


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
