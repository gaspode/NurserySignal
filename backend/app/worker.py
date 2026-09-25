from __future__ import annotations

import json
from typing import Any

from app.ai_shadow import evaluate_shadow
from app.config import Settings
from app.enrichment import fixture_enrichment
from app.logging import configure_logging
from app.queueing import EnrichmentMessage
from app.repository import (
    ai_review_exists,
    correlate_signal,
    get_raw_signal,
    save_ai_review,
    save_enrichment,
)

logger = configure_logging()


def process_message(settings: Settings, body: str) -> None:
    payload = json.loads(body)
    message = EnrichmentMessage.from_dict(payload)
    raw = get_raw_signal(settings, message.signal_id)
    if raw is None:
        raise ValueError("enrichment message references an unknown signal")
    candidate = fixture_enrichment(raw)
    created = save_enrichment(settings, candidate)
    candidate["metadata"] = {**(raw.get("metadata") or {}), "source_type": raw.get("source_type")}
    opportunity = {"opportunity_id": "disabled", "linked": False}
    if getattr(settings, "database_url", None) or getattr(settings, "db_secret_arn", None):
        opportunity = correlate_signal(settings, message.signal_id, candidate)
    logger.info(
        "enrichment signal_id=%s created=%s opportunity_id=%s linked=%s",
        message.signal_id,
        created,
        opportunity["opportunity_id"],
        opportunity["linked"],
    )
    if getattr(settings, "ai_shadow_enabled", False):
        model_id = getattr(settings, "ai_model_id", "eu.amazon.nova-lite-v1:0")
        prompt_version = getattr(settings, "ai_prompt_version", "shadow-v1")
        if not ai_review_exists(settings, message.signal_id, model_id, prompt_version):
            save_ai_review(settings, message.signal_id, evaluate_shadow(raw, settings))


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
