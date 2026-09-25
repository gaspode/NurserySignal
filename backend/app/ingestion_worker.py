from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
from app.ingestion import NormalizedSignal
from app.queueing import SignalIngestionMessage
from app.service import ingest_signal

logger = logging.getLogger("nurserysignal.ingestion_worker")


def process_message(settings: Settings, body: str) -> None:
    message = SignalIngestionMessage.from_json(body.encode())
    signal = NormalizedSignal.from_dict(message.signal)
    evidence = json.dumps(
        message.raw_provider_record, separators=(",", ":"), sort_keys=True
    ).encode()
    ingest_signal(settings, signal, evidence)
    logger.info(
        "signal_ingested source_type=%s external_id=%s", signal.source_type, signal.external_id
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    settings = Settings.from_env()
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            process_message(settings, str(record["body"]))
        except Exception:
            logger.exception("planning_ingestion_failed message_id=%s", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
