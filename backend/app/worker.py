from __future__ import annotations

import json
from typing import Any

from app.ai_shadow import SUPPORTED_SHADOW_SOURCE_TYPES, evaluate_shadow, prompt_version_for
from app.config import Settings
from app.logging import configure_logging
from app.queueing import EnrichmentMessage
from app.repository import (
    ai_review_exists,
    apply_safe_approval_policy,
    auto_reject_refused_planning,
    correlate_signal,
    get_raw_signal,
    save_ai_review,
    save_enrichment,
)
from app.review_triage import planning_refusal_assessment
from app.verticals import policy_for

logger = configure_logging()


def process_message(settings: Settings, body: str) -> None:
    payload = json.loads(body)
    message = EnrichmentMessage.from_dict(payload)
    raw = get_raw_signal(settings, message.signal_id)
    if raw is None:
        raise ValueError("enrichment message references an unknown signal")
    candidate = policy_for(str(raw.get("vertical") or "NURSERY")).classify_signal(raw)
    candidate["metadata"] = {
        **(raw.get("metadata") or {}),
        "source_type": raw.get("source_type"),
        "vertical": raw.get("vertical", "NURSERY"),
    }
    created = save_enrichment(settings, candidate)
    opportunity = {"opportunity_id": "disabled", "linked": False}
    refused = False
    if raw.get("source_type") == "planning":
        refused = planning_refusal_assessment(raw.get("metadata")).refused
        if refused:
            auto_reject_refused_planning(
                settings,
                message.signal_id,
                raw.get("metadata"),
            )
    if not refused and (
        getattr(settings, "database_url", None) or getattr(settings, "db_secret_arn", None)
    ):
        opportunity = correlate_signal(settings, message.signal_id, candidate)
    logger.info(
        "enrichment signal_id=%s created=%s refused=%s opportunity_id=%s linked=%s",
        message.signal_id,
        created,
        refused,
        opportunity["opportunity_id"],
        opportunity["linked"],
    )
    if (
        getattr(settings, "ai_shadow_enabled", False)
        and raw.get("source_type") in SUPPORTED_SHADOW_SOURCE_TYPES
    ):
        model_id = getattr(settings, "ai_model_id", "eu.amazon.nova-lite-v1:0")
        source_type = str(raw.get("source_type") or "").lower()
        prompt_version = prompt_version_for(source_type, settings, raw.get("vertical"))
        if not ai_review_exists(settings, message.signal_id, model_id, prompt_version):
            review = evaluate_shadow(raw, settings)
            saved = save_ai_review(settings, message.signal_id, review)
            if saved and review.get("status") == "SUCCEEDED":
                apply_safe_approval_policy(settings, message.signal_id)


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
