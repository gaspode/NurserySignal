import json

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
