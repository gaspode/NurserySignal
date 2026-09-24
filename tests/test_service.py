from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.config import Settings
from app.ingestion import NormalizedSignal
from app.repository import SignalIdentity, StoredSignal
from app.service import EnrichmentQueueError, SignalConflictError, ingest_signal
from app.storage import EvidencePersistenceError


def signal() -> NormalizedSignal:
    return NormalizedSignal.from_dict(
        {
            "source_type": "planning",
            "source_url": "https://example.test/planning/1",
            "external_id": "planning-1",
            "discovered_at": datetime.now(UTC).isoformat(),
            "title": "New nursery",
            "raw_text": "A nursery is proposed.",
        }
    )


def settings() -> Settings:
    return Settings(evidence_bucket="evidence", enrichment_queue_url="https://sqs.test/queue")


def stored(
    queued: datetime | None = None, created: bool = True, content_hash: str | None = None
) -> StoredSignal:
    return StoredSignal(
        SignalIdentity(uuid4(), "signals/planning/raw.json", queued, content_hash),
        created,
    )


def test_s3_failure_prevents_db_insert(monkeypatch) -> None:
    monkeypatch.setattr("app.service.find_signal", lambda *args: None)
    monkeypatch.setattr(
        "app.service.put_raw_evidence",
        lambda *args: (_ for _ in ()).throw(EvidencePersistenceError("failed")),
    )
    with pytest.raises(EvidencePersistenceError):
        ingest_signal(settings(), signal(), b"{}")


def test_successful_new_signal_persists_then_queues(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("app.service.find_signal", lambda *args: None)
    monkeypatch.setattr("app.service.put_raw_evidence", lambda *args: calls.append("s3"))
    monkeypatch.setattr("app.service.store_signal", lambda *args: calls.append("db") or stored())
    monkeypatch.setattr(
        "app.service.dispatch_enrichment", lambda *args: calls.append("sqs") or True
    )
    result = ingest_signal(settings(), signal(), b"{}")
    assert result.status == "accepted"
    assert calls == ["s3", "db", "sqs"]


def test_duplicate_does_not_write_evidence_or_queue(monkeypatch) -> None:
    existing = SignalIdentity(uuid4(), "signals/planning/raw.json", datetime.now(UTC), None)
    monkeypatch.setattr("app.service.find_signal", lambda *args: existing)
    monkeypatch.setattr(
        "app.service.store_signal", lambda *args: stored(existing.enrichment_queued_at, False)
    )
    monkeypatch.setattr(
        "app.service.put_raw_evidence", lambda *args: pytest.fail("duplicate wrote S3")
    )
    monkeypatch.setattr(
        "app.service.dispatch_enrichment", lambda *args: pytest.fail("duplicate queued")
    )
    result = ingest_signal(settings(), signal(), b"{}")
    assert result.status == "duplicate"


def test_sqs_failure_keeps_signal_error_explicit(monkeypatch) -> None:
    monkeypatch.setattr("app.service.find_signal", lambda *args: None)
    monkeypatch.setattr("app.service.put_raw_evidence", lambda *args: None)
    monkeypatch.setattr("app.service.store_signal", lambda *args: stored())
    monkeypatch.setattr(
        "app.service.dispatch_enrichment",
        lambda *args: (_ for _ in ()).throw(RuntimeError("sqs down")),
    )
    with pytest.raises(EnrichmentQueueError):
        ingest_signal(settings(), signal(), b"{}")


def test_conflicting_duplicate_is_rejected(monkeypatch) -> None:
    existing = SignalIdentity(uuid4(), "signals/planning/raw.json", None, "different")
    monkeypatch.setattr("app.service.find_signal", lambda *args: existing)
    with pytest.raises(SignalConflictError):
        ingest_signal(settings(), signal(), b"{}")
