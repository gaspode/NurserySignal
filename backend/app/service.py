from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.ingestion import NormalizedSignal
from app.repository import dispatch_enrichment, find_signal, store_signal
from app.storage import evidence_key, evidence_sha256, put_raw_evidence


class SignalConflictError(ValueError):
    """The same source identity was submitted with different content."""


class EnrichmentQueueError(RuntimeError):
    """The signal was stored but its enrichment message was not queued."""


@dataclass(frozen=True)
class IngestionResult:
    signal_id: str
    status: str
    evidence_key: str
    enrichment_queued: bool


def ingest_signal(
    settings: Settings,
    signal: NormalizedSignal,
    original_payload: bytes,
) -> IngestionResult:
    if not settings.evidence_bucket:
        raise RuntimeError("EVIDENCE_BUCKET is not configured")
    content_hash = evidence_sha256(original_payload)
    existing = find_signal(settings, signal.source_type, signal.external_id)
    key = (
        existing.evidence_key
        if existing and existing.evidence_key
        else evidence_key(signal, original_payload)
    )

    if existing and existing.content_sha256 and existing.content_sha256 != content_hash:
        raise SignalConflictError("source_type and external_id already identify different content")
    if existing is None:
        put_raw_evidence(settings, settings.evidence_bucket, key, original_payload)

    stored = store_signal(settings, signal, settings.evidence_bucket, key, content_hash)
    if stored.identity.content_sha256 and stored.identity.content_sha256 != content_hash:
        raise SignalConflictError("source_type and external_id already identify different content")

    queued = False
    if stored.identity.enrichment_queued_at is None:
        try:
            queued = dispatch_enrichment(
                settings,
                stored.identity,
                signal.schema_version,
                settings.evidence_bucket,
                stored.identity.evidence_key or key,
            )
        except Exception as exc:
            raise EnrichmentQueueError("signal stored but enrichment queueing failed") from exc
    return IngestionResult(
        signal_id=str(stored.identity.id),
        status="accepted" if stored.created else "duplicate",
        evidence_key=stored.identity.evidence_key or key,
        enrichment_queued=queued or stored.identity.enrichment_queued_at is not None,
    )


def parse_json_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def request_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()
