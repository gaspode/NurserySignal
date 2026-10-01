from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import boto3

from app.config import Settings
from app.verticals import NURSERY, validate_vertical


@dataclass(frozen=True)
class EnrichmentMessage:
    message_version: str
    signal_id: str
    schema_version: str
    evidence_bucket: str
    evidence_key: str
    queued_at: str
    vertical: str = NURSERY

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> EnrichmentMessage:
        required = (
            "message_version",
            "signal_id",
            "schema_version",
            "evidence_bucket",
            "evidence_key",
            "queued_at",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"missing enrichment message fields: {', '.join(missing)}")
        if str(payload["message_version"]) != "1.0":
            raise ValueError("unsupported enrichment message version")
        datetime.fromisoformat(str(payload["queued_at"]).replace("Z", "+00:00"))
        values = {key: str(payload[key]) for key in required}
        values["vertical"] = validate_vertical(str(payload.get("vertical") or NURSERY))
        return cls(**values)


@dataclass(frozen=True)
class PlanningOriginRecoveryResultMessage:
    """Collector-to-enrichment callback for a targeted Planning origin lookup."""

    message_version: str
    message_type: str
    attempt_id: str
    status: str
    details: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> PlanningOriginRecoveryResultMessage:
        if payload.get("message_version") != "1.0":
            raise ValueError("unsupported Planning origin result message version")
        if payload.get("message_type") != "planning_origin_recovery_result":
            raise ValueError("unsupported Planning origin result message type")
        attempt_id = str(payload.get("attempt_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not attempt_id:
            raise ValueError("missing Planning origin recovery attempt ID")
        if status not in {"FOUND", "NOT_FOUND", "AMBIGUOUS", "PROVIDER_ERROR"}:
            raise ValueError("invalid Planning origin recovery result status")
        details = payload.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("invalid Planning origin recovery result details")
        return cls("1.0", "planning_origin_recovery_result", attempt_id, status, details)


@dataclass(frozen=True)
class PlanningLifecycleWatchResultMessage:
    """Collector-to-enrichment callback for one bounded lifecycle-watch poll."""

    message_version: str
    message_type: str
    run_id: str
    watch_id: str
    status: str
    details: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> PlanningLifecycleWatchResultMessage:
        if payload.get("message_version") != "1.0":
            raise ValueError("unsupported Planning watch result message version")
        if payload.get("message_type") != "planning_lifecycle_watch_result":
            raise ValueError("unsupported Planning watch result message type")
        run_id = str(payload.get("run_id") or "").strip()
        watch_id = str(payload.get("watch_id") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not run_id or not watch_id:
            raise ValueError("missing Planning watch result identity")
        if status not in {"UNCHANGED", "CHANGED", "FAILED", "RATE_LIMITED"}:
            raise ValueError("invalid Planning watch result status")
        details = payload.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("invalid Planning watch result details")
        return cls(
            "1.0", "planning_lifecycle_watch_result", run_id, watch_id, status, details
        )


@dataclass(frozen=True)
class SignalIngestionMessage:
    """Small, versioned collector-to-ingestion contract."""

    message_version: str
    signal: dict[str, Any]
    raw_provider_record: dict[str, Any]
    ofsted_urn_enrichment: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, body: bytes) -> SignalIngestionMessage:
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("message_version") != "1.0":
            raise ValueError("unsupported ingestion message version")
        signal = payload.get("signal")
        raw = payload.get("raw_provider_record")
        if not isinstance(signal, dict) or not isinstance(raw, dict):
            raise ValueError("invalid ingestion message payload")
        ofsted_enrichment = payload.get("ofsted_urn_enrichment")
        if ofsted_enrichment is not None and not isinstance(ofsted_enrichment, dict):
            raise ValueError("invalid Ofsted URN enrichment payload")
        return cls("1.0", signal, raw, ofsted_enrichment)


def send_ingestion_message(settings: Settings, message: SignalIngestionMessage) -> str:
    if not settings.ingestion_queue_url:
        raise RuntimeError("INGESTION_QUEUE_URL is not configured")
    response = boto3.client("sqs").send_message(
        QueueUrl=settings.ingestion_queue_url,
        MessageBody=json.dumps(asdict(message), separators=(",", ":"), sort_keys=True),
    )
    return str(response["MessageId"])


def send_enrichment_message(settings: Settings, message: EnrichmentMessage) -> str:
    if not settings.enrichment_queue_url:
        raise RuntimeError("ENRICHMENT_QUEUE_URL is not configured")
    response = boto3.client("sqs").send_message(
        QueueUrl=settings.enrichment_queue_url,
        MessageBody=message.to_json(),
    )
    return str(response["MessageId"])


def send_planning_origin_recovery_result(
    settings: Settings, message: PlanningOriginRecoveryResultMessage
) -> str:
    if not settings.enrichment_queue_url:
        raise RuntimeError("ENRICHMENT_QUEUE_URL is not configured")
    response = boto3.client("sqs").send_message(
        QueueUrl=settings.enrichment_queue_url,
        MessageBody=message.to_json(),
    )
    return str(response["MessageId"])


def send_planning_lifecycle_watch_result(
    settings: Settings, message: PlanningLifecycleWatchResultMessage
) -> str:
    if not settings.enrichment_queue_url:
        raise RuntimeError("ENRICHMENT_QUEUE_URL is not configured")
    response = boto3.client("sqs").send_message(
        QueueUrl=settings.enrichment_queue_url,
        MessageBody=message.to_json(),
    )
    return str(response["MessageId"])
