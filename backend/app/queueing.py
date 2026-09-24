from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

import boto3

from app.config import Settings


@dataclass(frozen=True)
class EnrichmentMessage:
    message_version: str
    signal_id: str
    schema_version: str
    evidence_bucket: str
    evidence_key: str
    queued_at: str

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
        return cls(**{key: str(payload[key]) for key in required})


def send_enrichment_message(settings: Settings, message: EnrichmentMessage) -> str:
    if not settings.enrichment_queue_url:
        raise RuntimeError("ENRICHMENT_QUEUE_URL is not configured")
    response = boto3.client("sqs").send_message(
        QueueUrl=settings.enrichment_queue_url,
        MessageBody=message.to_json(),
    )
    return str(response["MessageId"])
