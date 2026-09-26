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


def compare_bounded_recruitment_ai_reviews(
    settings: Settings, *, limit: int
) -> dict[str, Any]:
    if not 1 <= limit <= 25:
        raise ValueError("limit must be between 1 and 25")
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.title, rs.external_id, se.review_status,
                   v2.recommendation, v2.confidence, v2.reason,
                   v3.recommendation, v3.confidence, v3.reason,
                   v3.recruitment_relevance, v3.commercial_change_evidence
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN signal_ai_reviews v2
              ON v2.raw_signal_id = rs.id AND v2.provider = 'BEDROCK'
             AND v2.prompt_version = 'shadow-v2'
            LEFT JOIN signal_ai_reviews v3
              ON v3.raw_signal_id = rs.id AND v3.provider = 'BEDROCK'
             AND v3.prompt_version = %s
            WHERE rs.source_type = 'recruitment' AND v3.id IS NOT NULL
            ORDER BY rs.discovered_at, rs.id LIMIT %s
            """,
            (settings.ai_prompt_version, limit),
        ).fetchall()
    results = []
    for row in rows:
        results.append(
            {
                "title": row[0],
                "external_id": row[1],
                "review_status": row[2],
                "v2": {"recommendation": row[3], "confidence": row[4], "reason": row[5]},
                "v3": {
                    "recommendation": row[6],
                    "confidence": row[7],
                    "reason": row[8],
                    "recruitment_relevance": row[9],
                    "commercial_change_evidence": row[10],
                },
            }
        )
    return {"count": len(results), "results": results}
