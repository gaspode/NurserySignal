import json
from types import SimpleNamespace

from app.ingestion_worker import handler


def test_poison_ingestion_message_is_returned_for_dlq(monkeypatch) -> None:
    monkeypatch.setattr("app.ingestion_worker.Settings.from_env", lambda: object())
    result = handler({"Records": [{"messageId": "poison-1", "body": "not-json"}]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "poison-1"}]}


def test_ingestion_worker_passes_canonical_signal_and_provider_evidence(monkeypatch) -> None:
    monkeypatch.setattr("app.ingestion_worker.Settings.from_env", lambda: object())
    captured = []
    monkeypatch.setattr(
        "app.ingestion_worker.ingest_signal",
        lambda settings, signal, evidence: captured.append((signal, evidence)),
    )
    body = json.dumps(
        {
            "message_version": "1.0",
            "signal": {
                "source_type": "planning",
                "source_url": "https://council.example/app/1",
                "external_id": "plota:1",
                "discovered_at": "2026-09-24T08:00:00Z",
                "title": "Change of use to a nursery",
                "raw_text": "Change of use to a nursery",
            },
            "raw_provider_record": {"id": "1", "description": "Change of use to a nursery"},
        }
    )
    result = handler({"Records": [{"messageId": "good-1", "body": body}]}, None)
    assert result == {"batchItemFailures": []}
    assert captured[0][0].external_id == "plota:1"
    assert json.loads(captured[0][1])["id"] == "1"


def test_ingestion_worker_routes_organisation_enrichment_without_signal_ingestion(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.ingestion_worker.Settings.from_env", lambda: object())
    captured = []
    monkeypatch.setattr(
        "app.ingestion_worker.process_organisation_enrichment",
        lambda settings, payload: captured.append(payload)
        or {"operator_id": payload["operator_id"], "status": "MATCHED"},
    )
    monkeypatch.setattr(
        "app.ingestion_worker.ingest_signal",
        lambda *args: (_ for _ in ()).throw(AssertionError("signal ingestion must not run")),
    )
    body = json.dumps(
        {
            "message_type": "organisation_enrichment",
            "provider": "COMPANIES_HOUSE",
            "operator_id": "operator-1",
            "query_name": "Acme Care Limited",
        }
    )
    result = handler({"Records": [{"messageId": "org-1", "body": body}]}, None)
    assert result == {"batchItemFailures": []}
    assert captured[0]["provider"] == "COMPANIES_HOUSE"


def test_ingestion_worker_persists_ofsted_urn_enrichment_after_base_signal(
    monkeypatch,
) -> None:
    settings = object()
    monkeypatch.setattr("app.ingestion_worker.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "app.ingestion_worker.ingest_signal",
        lambda *_args: SimpleNamespace(signal_id="signal-1"),
    )
    persisted = []
    monkeypatch.setattr(
        "app.ingestion_worker.persist_ofsted_urn_enrichment",
        lambda current, signal_id, value: persisted.append((current, signal_id, value)),
    )
    body = json.dumps(
        {
            "message_version": "1.0",
            "signal": {
                "source_type": "ofsted",
                "source_url": "https://reports.ofsted.gov.uk/provider/2/2766766",
                "external_id": "ofsted:2766766",
                "discovered_at": "2026-09-27T08:00:00Z",
                "title": "Ofsted registration — Oaktree Childcare Ltd",
                "raw_text": "Registration status: Active",
                "vertical": "CHILDRENS_HOME",
            },
            "raw_provider_record": {"URN": "2766766"},
            "ofsted_urn_enrichment": {
                "urn": "2766766",
                "status": "SUCCEEDED",
                "registered_provider_name": "Oaktree Childcare Limited",
            },
        }
    )
    result = handler({"Records": [{"messageId": "ofsted-1", "body": body}]}, None)
    assert result == {"batchItemFailures": []}
    assert persisted[0][1] == "signal-1"
    assert persisted[0][2]["registered_provider_name"] == "Oaktree Childcare Limited"
