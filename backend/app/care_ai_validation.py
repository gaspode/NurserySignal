from __future__ import annotations

from typing import Any
from uuid import UUID

from app.ai_shadow import CARE_PLANNING_PROMPT_VERSION, evaluate_shadow
from app.care_planning_review import (
    care_planning_fastpath_qa_holdout,
    planning_withdrawal_assessment,
)
from app.config import Settings
from app.db import connection
from app.repository import (
    get_ai_review,
    get_raw_signal,
    get_signal_review_status,
    record_admin_audit,
    save_ai_review,
)
from app.review_triage import (
    deterministic_review_recommendation,
    planning_refusal_assessment,
)

CARE_PLANNING_PREVIOUS_PROMPT_VERSION = "care-planning-shadow-v1"
CARE_AI_POLICY_PREVIEW_VERSION = "care-planning-ai-policy-preview-v1"
CARE_AI_MIN_CONFIDENCE = 0.95
CARE_AI_REFRESH_MAX_BATCH = 10
CARE_AI_REFRESH_FAILURE_STOP = 3
_POLICY_EXCLUDED_SUBTYPES = {
    "LAWFULNESS_EXISTING",
    "CONDITION_DISCHARGE",
    "NON_MATERIAL_AMENDMENT",
}


def _rows(settings: Settings) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.id, rs.title, rs.metadata, rs.discovered_at,
                   se.review_status, se.reviewed_by, se.reviewed_at, se.extracted_facts,
                   v1.status, v1.recommendation, v1.confidence, v1.reason,
                   v2.status, v2.recommendation, v2.confidence, v2.reason,
                   latest.status, latest.recommendation, latest.confidence,
                   latest.prompt_version
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, reason
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id AND provider = 'BEDROCK'
                  AND prompt_version = %s
                ORDER BY created_at DESC LIMIT 1
            ) v1 ON TRUE
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, reason
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id AND provider = 'BEDROCK'
                  AND prompt_version = %s
                ORDER BY created_at DESC LIMIT 1
            ) v2 ON TRUE
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id AND provider = 'BEDROCK'
                ORDER BY created_at DESC LIMIT 1
            ) latest ON TRUE
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
            ORDER BY se.reviewed_at DESC NULLS LAST, rs.discovered_at DESC, rs.id DESC
            LIMIT 5000
            """,
            (CARE_PLANNING_PREVIOUS_PROMPT_VERSION, CARE_PLANNING_PROMPT_VERSION),
        ).fetchall()
    fields = (
        "id",
        "title",
        "metadata",
        "discovered_at",
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "extracted_facts",
        "v1_status",
        "v1_recommendation",
        "v1_confidence",
        "v1_reason",
        "v2_status",
        "v2_recommendation",
        "v2_confidence",
        "v2_reason",
        "latest_status",
        "latest_recommendation",
        "latest_confidence",
        "latest_prompt_version",
    )
    return [dict(zip(fields, row)) for row in rows]


def _human_reviewed(item: dict[str, Any]) -> bool:
    return item["review_status"] in {"APPROVED", "REJECTED"} and not str(
        item.get("reviewed_by") or ""
    ).startswith("system:")


def _structured_negative(item: dict[str, Any]) -> bool:
    return planning_refusal_assessment(item.get("metadata")).refused or (
        planning_withdrawal_assessment(item.get("metadata")).withdrawn
    )


def care_planning_ai_currency(item: dict[str, Any]) -> str:
    """Classify the latest immutable assessment used by the Review Inbox."""
    if not item.get("latest_prompt_version"):
        return "NO_AI_ASSESSMENT"
    if item.get("latest_status") == "FAILED":
        return "AI_FAILED"
    if item.get("latest_prompt_version") == CARE_PLANNING_PROMPT_VERSION:
        return "CURRENT_V2"
    return "STALE_V1"


def _pending_snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    pending = [item for item in items if item["review_status"] == "PENDING"]
    currency = {
        key: sum(care_planning_ai_currency(item) == key for item in pending)
        for key in ("CURRENT_V2", "STALE_V1", "NO_AI_ASSESSMENT", "AI_FAILED")
    }
    subtypes: dict[str, int] = {}
    recommendations: dict[str, int] = {}
    confidence: dict[str, int] = {}
    recommendation_subtypes: dict[str, dict[str, int]] = {}
    for item in pending:
        subtype = str((item.get("extracted_facts") or {}).get("planning_subtype") or "AMBIGUOUS")
        subtypes[subtype] = subtypes.get(subtype, 0) + 1
        if care_planning_ai_currency(item) != "CURRENT_V2":
            continue
        recommendation = str(item.get("latest_recommendation") or "NEEDS_HUMAN")
        recommendations[recommendation] = recommendations.get(recommendation, 0) + 1
        confidence_key = (
            f"{float(item['latest_confidence']):.2f}"
            if item.get("latest_confidence") is not None
            else "NONE"
        )
        confidence[confidence_key] = confidence.get(confidence_key, 0) + 1
        by_subtype = recommendation_subtypes.setdefault(recommendation, {})
        by_subtype[subtype] = by_subtype.get(subtype, 0) + 1
    return {
        "prompt_version": CARE_PLANNING_PROMPT_VERSION,
        "pending_total": len(pending),
        "currency": currency,
        "subtypes": subtypes,
        "v2_recommendations": recommendations,
        "v2_confidence_distribution": confidence,
        "v2_recommendations_by_subtype": recommendation_subtypes,
    }


def care_planning_ai_refresh_preview(settings: Settings) -> dict[str, Any]:
    """Return current pending AI currency and review distributions without mutation."""
    return _pending_snapshot(_rows(settings))


def refresh_stale_care_planning_ai(
    settings: Settings,
    *,
    actor: str,
    limit: int = CARE_AI_REFRESH_MAX_BATCH,
    include_missing: bool = True,
) -> dict[str, Any]:
    """Append v2 assessments for a bounded stale/missing pending CareProspect cohort."""
    bounded_limit = min(max(int(limit), 1), CARE_AI_REFRESH_MAX_BATCH)
    allowed = {"STALE_V1"}
    if include_missing:
        allowed.add("NO_AI_ASSESSMENT")
    before = _rows(settings)
    selected = [
        item
        for item in before
        if item["review_status"] == "PENDING" and care_planning_ai_currency(item) in allowed
    ][:bounded_limit]
    results: list[dict[str, Any]] = []
    consecutive_failures = 0
    stopped_early = False
    for item in selected:
        signal_id = str(item["id"])
        raw = get_raw_signal(settings, signal_id)
        if raw is None:
            results.append({"signal_id": signal_id, "status": "NOT_FOUND"})
            consecutive_failures += 1
        else:
            # A concurrent refresh may have completed after cohort selection.
            existing = get_ai_review(
                settings, signal_id, settings.ai_model_id, CARE_PLANNING_PROMPT_VERSION
            )
            if existing is not None:
                results.append(
                    {"signal_id": signal_id, "status": "SKIPPED_CURRENT", "idempotent": True}
                )
                consecutive_failures = 0
                continue
            review = evaluate_shadow(raw, settings)
            saved = save_ai_review(settings, signal_id, review)
            status = str(review.get("status") or "FAILED")
            structured_correction = (
                item.get("latest_recommendation") == "APPROVE"
                and review.get("recommendation") == "REJECT"
                and _structured_negative(item)
            )
            results.append(
                {
                    "signal_id": signal_id,
                    "status": status,
                    "saved": saved,
                    "previous_prompt_version": item.get("latest_prompt_version"),
                    "previous_recommendation": item.get("latest_recommendation"),
                    "v2_recommendation": review.get("recommendation"),
                    "v2_confidence": review.get("confidence"),
                    "failure_category": review.get("failure_category"),
                    "structured_negative_correction": structured_correction,
                }
            )
            consecutive_failures = 0 if status == "SUCCEEDED" else consecutive_failures + 1
        if consecutive_failures >= CARE_AI_REFRESH_FAILURE_STOP:
            stopped_early = True
            break
    summary = {
        "prompt_version": CARE_PLANNING_PROMPT_VERSION,
        "batch_limit": bounded_limit,
        "selected": len(selected),
        "attempted": len(results),
        "succeeded": sum(item.get("status") == "SUCCEEDED" for item in results),
        "failed": sum(
            item.get("status") not in {"SUCCEEDED", "SKIPPED_CURRENT"} for item in results
        ),
        "idempotent_skips": sum(item.get("status") == "SKIPPED_CURRENT" for item in results),
        "structured_negative_corrections": sum(
            bool(item.get("structured_negative_correction")) for item in results
        ),
        "stopped_early": stopped_early,
        "review_decisions_mutated": False,
        "customer_publication_unchanged": True,
        "items": results,
    }
    record_admin_audit(
        settings,
        action="CARE_PLANNING_AI_V2_STALE_REFRESH",
        actor=actor,
        target_type="signal",
        details={key: value for key, value in summary.items() if key != "items"},
    )
    return summary


def _v1_disagreement(item: dict[str, Any]) -> bool:
    deterministic = deterministic_review_recommendation(item.get("extracted_facts"))
    return (
        item.get("v1_status") == "SUCCEEDED"
        and item.get("v1_recommendation") in {"APPROVE", "REJECT"}
        and deterministic in {"APPROVE", "REJECT"}
        and item["v1_recommendation"] != deterministic
    )


def care_ai_policy_preview_candidate(item: dict[str, Any]) -> bool:
    facts = item.get("extracted_facts") or {}
    return (
        item.get("review_status") == "PENDING"
        and not item.get("reviewed_by")
        and item.get("latest_status") == "SUCCEEDED"
        and item.get("latest_recommendation") == "APPROVE"
        and float(item.get("latest_confidence") or 0) >= CARE_AI_MIN_CONFIDENCE
        and not _structured_negative(item)
        and facts.get("likely_false_positive") is not True
        and not (facts.get("planning_ambiguity_markers") or [])
        and str(facts.get("planning_subtype") or "AMBIGUOUS") not in _POLICY_EXCLUDED_SUBTYPES
    )


def _human_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    cohort = [item for item in items if _human_reviewed(item) and _v1_disagreement(item)]
    agree = sum(
        (item["v1_recommendation"] == "APPROVE") == (item["review_status"] == "APPROVED")
        for item in cohort
    )
    negatives = [item for item in cohort if _structured_negative(item)]
    adjusted = [item for item in cohort if not _structured_negative(item)]
    adjusted_agree = sum(
        (item["v1_recommendation"] == "APPROVE") == (item["review_status"] == "APPROVED")
        for item in adjusted
    )
    return {
        "reviewed_disagreement_records": len(cohort),
        "ai_human_agreement": agree,
        "ai_human_agreement_rate": agree / len(cohort) if cohort else None,
        "false_approves": sum(
            item["v1_recommendation"] == "APPROVE" and item["review_status"] == "REJECTED"
            for item in cohort
        ),
        "false_rejects": sum(
            item["v1_recommendation"] == "REJECT" and item["review_status"] == "APPROVED"
            for item in cohort
        ),
        "explicit_negative_errors": sum(
            item["v1_recommendation"] == "APPROVE" for item in negatives
        ),
        "agreement_excluding_structured_negative_defect": adjusted_agree,
        "agreement_rate_excluding_structured_negative_defect": (
            adjusted_agree / len(adjusted) if adjusted else None
        ),
        "caveat": (
            "Historical review rows do not snapshot every deterministic policy input at "
            "decision time; this is a current-state reconstruction of the reviewed cohort."
        ),
    }


def care_planning_ai_validation_preview(
    settings: Settings, *, sample_limit: int = 80
) -> dict[str, Any]:
    bounded_limit = min(max(int(sample_limit), 1), 100)
    items = _rows(settings)
    refusal_candidates = [
        item
        for item in items
        if _structured_negative(item)
        and item.get("v1_status") == "SUCCEEDED"
        and item.get("v1_recommendation") == "APPROVE"
    ][:25]
    disagreement = [item for item in items if _v1_disagreement(item)][:bounded_limit]
    pending = [item for item in items if item["review_status"] == "PENDING"]
    eligible = [item for item in pending if care_ai_policy_preview_candidate(item)]
    subtype_counts: dict[str, int] = {}
    version_counts: dict[str, int] = {}
    for item in eligible:
        facts = item.get("extracted_facts") or {}
        subtype = str(facts.get("planning_subtype") or "AMBIGUOUS")
        subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
        version = str(item.get("latest_prompt_version") or "NONE")
        version_counts[version] = version_counts.get(version, 0) + 1
    holdouts = sum(care_planning_fastpath_qa_holdout(str(item["id"])) for item in eligible)
    return {
        "prompt_version": CARE_PLANNING_PROMPT_VERSION,
        "historical_prompt_version": CARE_PLANNING_PREVIOUS_PROMPT_VERSION,
        "refusal_candidates": [
            {
                "signal_id": str(item["id"]),
                "title": item["title"],
                "review_status": item["review_status"],
            }
            for item in refusal_candidates
        ],
        "disagreement_sample": [
            {
                "signal_id": str(item["id"]),
                "review_status": item["review_status"],
                "human_reviewed": _human_reviewed(item),
                "v1_recommendation": item["v1_recommendation"],
                "deterministic_recommendation": deterministic_review_recommendation(
                    item.get("extracted_facts")
                ),
            }
            for item in disagreement
        ],
        "human_validation": _human_metrics(items),
        "refresh": _pending_snapshot(items),
        "policy_preview": {
            "policy_version": CARE_AI_POLICY_PREVIEW_VERSION,
            "pending_evaluated": len(pending),
            "eligible": len(eligible),
            "qa_holdouts_at_10_percent": holdouts,
            "would_auto_approve": len(eligible) - holdouts,
            "remaining_manual": len(pending) - len(eligible) + holdouts,
            "subtypes": subtype_counts,
            "assessment_versions": version_counts,
            "preview_only": True,
            "customer_publication_unchanged": True,
        },
    }


def run_care_planning_ai_validation(
    settings: Settings, *, signal_ids: list[str], actor: str
) -> dict[str, Any]:
    if not 1 <= len(signal_ids) <= 5:
        raise ValueError("signal_ids must contain between 1 and 5 IDs")
    normalized_ids = [str(UUID(value)) for value in signal_ids]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise ValueError("duplicate_signal_id")
    results = []
    for signal_id in normalized_ids:
        raw = get_raw_signal(settings, signal_id)
        if raw is None:
            results.append({"signal_id": signal_id, "status": "NOT_FOUND"})
            continue
        if raw.get("vertical") != "CHILDRENS_HOME" or raw.get("source_type") != "planning":
            results.append({"signal_id": signal_id, "status": "OUT_OF_SCOPE"})
            continue
        v1 = get_ai_review(
            settings,
            signal_id,
            settings.ai_model_id,
            CARE_PLANNING_PREVIOUS_PROMPT_VERSION,
        )
        v2 = get_ai_review(settings, signal_id, settings.ai_model_id, CARE_PLANNING_PROMPT_VERSION)
        idempotent = v2 is not None
        if v2 is None:
            v2 = evaluate_shadow(raw, settings)
            save_ai_review(settings, signal_id, v2)
        results.append(
            {
                "signal_id": signal_id,
                "status": v2.get("status"),
                "idempotent": idempotent,
                "review_status": get_signal_review_status(settings, signal_id),
                "v1_recommendation": v1.get("recommendation") if v1 else None,
                "v2_recommendation": v2.get("recommendation"),
                "v2_confidence": v2.get("confidence"),
                "v2_reason": v2.get("reason"),
                "v2_planning_relevance": v2.get("planning_relevance"),
                "v2_change_evidence": v2.get("commercial_change_evidence"),
            }
        )
    record_admin_audit(
        settings,
        action="CARE_PLANNING_AI_V2_VALIDATION",
        actor=actor,
        target_type="signal",
        details={
            "prompt_version": CARE_PLANNING_PROMPT_VERSION,
            "requested": len(normalized_ids),
            "succeeded": sum(item.get("status") == "SUCCEEDED" for item in results),
            "review_decisions_mutated": False,
            "customer_publication_unchanged": True,
        },
    )
    return {
        "prompt_version": CARE_PLANNING_PROMPT_VERSION,
        "requested": len(normalized_ids),
        "succeeded": sum(item.get("status") == "SUCCEEDED" for item in results),
        "failed": sum(item.get("status") not in {"SUCCEEDED"} for item in results),
        "review_decisions_mutated": False,
        "customer_publication_unchanged": True,
        "items": results,
    }


def care_planning_ai_validation_report(
    settings: Settings, *, signal_ids: list[str]
) -> dict[str, Any]:
    if not 1 <= len(signal_ids) <= 100:
        raise ValueError("signal_ids must contain between 1 and 100 IDs")
    selected = {str(UUID(value)) for value in signal_ids}
    items = [item for item in _rows(settings) if str(item["id"]) in selected]
    succeeded = [item for item in items if item.get("v2_status") == "SUCCEEDED"]

    def agrees(item: dict[str, Any], version: str) -> bool | None:
        if not _human_reviewed(item):
            return None
        recommendation = item.get(f"{version}_recommendation")
        if recommendation not in {"APPROVE", "REJECT"}:
            return None
        return (recommendation == "APPROVE") == (item["review_status"] == "APPROVED")

    v1_labelled = [value for item in items if (value := agrees(item, "v1")) is not None]
    v2_labelled = [value for item in items if (value := agrees(item, "v2")) is not None]
    negatives = [item for item in succeeded if _structured_negative(item)]
    return {
        "prompt_version": CARE_PLANNING_PROMPT_VERSION,
        "requested": len(selected),
        "records_found": len(items),
        "v2_succeeded": len(succeeded),
        "v1_labelled": len(v1_labelled),
        "v1_agreement": sum(v1_labelled),
        "v2_labelled": len(v2_labelled),
        "v2_agreement": sum(v2_labelled),
        "v2_false_approves": sum(
            item.get("v2_recommendation") == "APPROVE" and item["review_status"] == "REJECTED"
            for item in succeeded
            if _human_reviewed(item)
        ),
        "v2_false_rejects": sum(
            item.get("v2_recommendation") == "REJECT" and item["review_status"] == "APPROVED"
            for item in succeeded
            if _human_reviewed(item)
        ),
        "structured_negative_tested": len(negatives),
        "structured_negative_rejected": sum(
            item.get("v2_recommendation") == "REJECT" for item in negatives
        ),
        "structured_negative_false_approves": sum(
            item.get("v2_recommendation") == "APPROVE" for item in negatives
        ),
        "review_decisions_mutated": False,
        "customer_publication_unchanged": True,
    }
