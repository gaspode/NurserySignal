from __future__ import annotations

from typing import Any

from app.ai_shadow import evaluate_shadow
from app.config import Settings
from app.repository import (
    connection,
    get_ai_review,
    reprocess_recruitment_signals,
    save_ai_review,
    signal_detail,
)
from app.recruitment import recruitment_record_from_signal


def run_bounded_recruitment_validation(
    settings: Settings, targets: list[dict[str, str]]
) -> dict[str, Any]:
    """One-off IAM-invoked validation for exactly three stored signals."""
    if len(targets) != 3:
        raise ValueError("exactly three recruitment validation targets are required")
    ids: list[str] = []
    with connection(settings) as conn:
        for target in targets:
            title = target.get("title")
            organisation = target.get("organisation")
            if not title or not organisation:
                raise ValueError("each target requires title and organisation")
            rows = conn.execute(
                """
                SELECT rs.id
                FROM raw_signals rs
                WHERE rs.source_type = 'recruitment'
                  AND rs.title ILIKE %s
                  AND rs.organisation_hint ILIKE %s
                ORDER BY rs.discovered_at DESC
                LIMIT 2
                """,
                (f"%{title}%", f"%{organisation}%"),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError(f"expected one stored signal for target {title}")
            ids.append(str(rows[0][0]))
    reprocess = reprocess_recruitment_signals(
        settings,
        actor="aws-iam-bounded-recruitment-validation",
        limit=3,
        signal_ids=ids,
    )
    results = []
    for signal_id in ids:
        existing = get_ai_review(
            settings, signal_id, settings.ai_model_id, settings.ai_prompt_version
        )
        if existing is None:
            review = evaluate_shadow(signal_detail(settings, signal_id) or {}, settings)
            save_ai_review(settings, signal_id, review)
        detail = signal_detail(settings, signal_id)
        enrichment = detail.get("enrichment") if detail else None
        facts = (enrichment or {}).get("extracted_facts") or {}
        recruitment_record = recruitment_record_from_signal(detail or {})
        ai_reviews = detail.get("ai_reviews", []) if detail else []
        results.append(
            {
                "id": signal_id,
                "title": detail.get("title") if detail else None,
                "external_id": detail.get("external_id") if detail else None,
                "organisation": detail.get("organisation_hint") if detail else None,
                "location": detail.get("location_hint") if detail else None,
                "review_status": (enrichment or {}).get("review_status"),
                "role": facts.get("recruitment_role_category"),
                "setting": facts.get("recruitment_setting_category"),
                "relevance": facts.get("recruitment_relevance"),
                "commercial_change_evidence": facts.get("commercial_change_evidence"),
                "postcode": (detail.get("metadata") or {}).get("postcode") if detail else None,
                "normalized_address": recruitment_record.address,
                "normalized_postcode": recruitment_record.postcode,
                "ai_reviews": [
                    {
                        "prompt_version": item.get("prompt_version"),
                        "recommendation": item.get("recommendation"),
                        "confidence": item.get("confidence"),
                        "reason": item.get("reason"),
                        "commercial_change_evidence": item.get("commercial_change_evidence"),
                        "status": item.get("status"),
                    }
                    for item in ai_reviews
                    if item.get("prompt_version") in {"shadow-v1", "shadow-v2"}
                ],
            }
        )
    return {"reprocess": reprocess, "signals": results}
