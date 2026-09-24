from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.enrichment import fixture_enrichment
from app.logging import configure_logging
from app.queueing import EnrichmentMessage
from app.repository import get_raw_signal, save_enrichment

logger = configure_logging()


def process_message(settings: Settings, body: str) -> None:
    payload = json.loads(body)
    message = EnrichmentMessage.from_dict(payload)
    raw = get_raw_signal(settings, message.signal_id)
    if raw is None:
        raise ValueError("enrichment message references an unknown signal")
    candidate = fixture_enrichment(raw)
    created = save_enrichment(settings, candidate)
    logger.info("enrichment signal_id=%s created=%s", message.signal_id, created)


def handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    settings = Settings.from_env()
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", "unknown"))
        try:
            process_message(settings, str(record["body"]))
        except Exception:
            logger.exception("enrichment_failed message_id=%s", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
