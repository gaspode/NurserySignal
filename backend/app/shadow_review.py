from __future__ import annotations

from typing import Any

from app.ai_shadow import evaluate_shadow
from app.config import Settings
from app.repository import (
    get_ai_review,
    get_raw_signal,
    get_signal_review_status,
    save_ai_review,
)


def _result(
    review: dict[str, Any], *, idempotent: bool, review_status: str | None
) -> dict[str, Any]:
    return {
        "recommendation": review.get("recommendation"),
        "confidence": review.get("confidence"),
        "reason": review.get("reason"),
        "commercial_change_evidence": review.get("commercial_change_evidence"),
        "model_id": review["model_id"],
        "prompt_version": review["prompt_version"],
        "status": review["status"],
        "failure_category": review.get("failure_category"),
        "evaluated_at": review.get("evaluated_at"),
        "attempted_at": review.get("attempted_at"),
        "latency_ms": review.get("latency_ms"),
        "idempotent": idempotent,
        "human_review_status": review_status,
    }


def reevaluate_ai_shadow(settings: Settings, signal_id: str) -> dict[str, Any] | None:
    """Evaluate stored evidence once for the configured model/prompt version."""
    raw = get_raw_signal(settings, signal_id)
    if raw is None:
        return None
    existing = get_ai_review(settings, signal_id, settings.ai_model_id, settings.ai_prompt_version)
    review_status = get_signal_review_status(settings, signal_id)
    if existing is not None:
        return _result(existing, idempotent=True, review_status=review_status)

    review = evaluate_shadow(raw, settings)
    saved = save_ai_review(settings, signal_id, review)
    if not saved:
        existing = get_ai_review(
            settings, signal_id, settings.ai_model_id, settings.ai_prompt_version
        )
        if existing is not None:
            return _result(existing, idempotent=True, review_status=review_status)
    return _result(review, idempotent=not saved, review_status=review_status)
