from __future__ import annotations

from typing import Any

from app.ai_shadow import evaluate_shadow
from app.config import Settings
from app.repository import (
    connection,
    get_ai_review,
    get_raw_signal,
    get_signal_review_status,
    save_ai_review,
)


def run_bounded_recruitment_ai_validation(
    settings: Settings, *, limit: int, signal_ids: list[str] | None = None
) -> dict[str, Any]:
    if not 1 <= limit <= 25:
        raise ValueError("limit must be between 1 and 25")
    with connection(settings) as conn:
        if signal_ids:
            rows = conn.execute(
                """
                SELECT rs.id FROM raw_signals rs
                LEFT JOIN signal_ai_reviews ai
                  ON ai.raw_signal_id = rs.id AND ai.provider = 'BEDROCK'
                 AND ai.model_id = %s AND ai.prompt_version = %s
                WHERE rs.source_type = 'recruitment' AND rs.id = ANY(%s::uuid[])
                  AND ai.id IS NULL
                ORDER BY rs.discovered_at, rs.id LIMIT %s
                """,
                (settings.ai_model_id, settings.ai_prompt_version, signal_ids, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT rs.id FROM raw_signals rs
                LEFT JOIN signal_ai_reviews ai
                  ON ai.raw_signal_id = rs.id AND ai.provider = 'BEDROCK'
                 AND ai.model_id = %s AND ai.prompt_version = %s
                WHERE rs.source_type = 'recruitment' AND ai.id IS NULL
                ORDER BY rs.discovered_at, rs.id LIMIT %s
                """,
                (settings.ai_model_id, settings.ai_prompt_version, limit),
            ).fetchall()
    results = []
    for (signal_id,) in rows:
        raw = get_raw_signal(settings, str(signal_id))
        if raw is None:
            continue
        review = evaluate_shadow(raw, settings)
        saved = save_ai_review(settings, str(signal_id), review)
        stored = get_ai_review(
            settings, str(signal_id), settings.ai_model_id, settings.ai_prompt_version
        )
        result = stored or review
        facts = (raw.get("metadata") or {}).get("recruitment_classification") or {}
        results.append(
            {
                "id": str(signal_id),
                "title": raw.get("title"),
                "external_id": raw.get("external_id"),
                "review_status": get_signal_review_status(settings, str(signal_id)),
                "deterministic_relevance": facts.get("relevance"),
                "recommendation": result.get("recommendation"),
                "confidence": result.get("confidence"),
                "reason": result.get("reason"),
                "recruitment_relevance": result.get("recruitment_relevance"),
                "commercial_change_evidence": result.get("commercial_change_evidence"),
                "prompt_version": result.get("prompt_version"),
                "status": result.get("status"),
                "saved": saved,
            }
        )
    return {"selected": len(rows), "results": results}
