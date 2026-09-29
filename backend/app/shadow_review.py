from __future__ import annotations

from typing import Any

from app.ai_shadow import SUPPORTED_SHADOW_SOURCE_TYPES, evaluate_shadow, prompt_version_for
from app.config import Settings
from app.repository import (
    apply_safe_approval_policy,
    get_ai_review,
    get_raw_signal,
    get_signal_review_status,
    save_ai_review,
)


class UnsupportedShadowSourceError(ValueError):
    pass


def _result(
    review: dict[str, Any], *, idempotent: bool, review_status: str | None
) -> dict[str, Any]:
    return {
        "recommendation": review.get("recommendation"),
        "confidence": review.get("confidence"),
        "reason": review.get("reason"),
        "recruitment_relevance": review.get("recruitment_relevance"),
        "planning_relevance": review.get("planning_relevance"),
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
    source_type = str(raw.get("source_type") or "").lower()
    if source_type not in SUPPORTED_SHADOW_SOURCE_TYPES:
        raise UnsupportedShadowSourceError(source_type)
    prompt_version = prompt_version_for(source_type, settings, raw.get("vertical"))
    existing = get_ai_review(settings, signal_id, settings.ai_model_id, prompt_version)
    if existing is not None:
        if existing.get("status") == "SUCCEEDED":
            apply_safe_approval_policy(settings, signal_id)
        return _result(
            existing,
            idempotent=True,
            review_status=get_signal_review_status(settings, signal_id),
        )

    review = evaluate_shadow(raw, settings)
    saved = save_ai_review(settings, signal_id, review)
    if not saved:
        existing = get_ai_review(settings, signal_id, settings.ai_model_id, prompt_version)
        if existing is not None:
            if existing.get("status") == "SUCCEEDED":
                apply_safe_approval_policy(settings, signal_id)
            return _result(
                existing,
                idempotent=True,
                review_status=get_signal_review_status(settings, signal_id),
            )
    if review.get("status") == "SUCCEEDED":
        apply_safe_approval_policy(settings, signal_id)
    return _result(
        review,
        idempotent=not saved,
        review_status=get_signal_review_status(settings, signal_id),
    )
