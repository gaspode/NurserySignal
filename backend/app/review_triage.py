from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

REFUSAL_POLICY_VERSION = "planning-refusal-v2"
SAFE_APPROVAL_MIN_CONFIDENCE = 0.95
REFUSED_DECISIONS = {
    "REFUSED",
    "REJECTED",
    "PERMISSION REFUSED",
    "APPLICATION REFUSED",
    "REFUSE PERMISSION CONSENT",
    "REFUSE PERMISSION",
    "REFUSE CONSENT",
    "REFUSAL OF PERMISSION",
    "REFUSAL OF CONSENT",
    "PERMISSION CONSENT REFUSED",
}
TRIAGE_BUCKETS = frozenset(
    {
        "SAFE_APPROVE_AGREEMENT",
        "DETERMINISTIC_AI_DISAGREE",
        "AI_UNCERTAIN",
        "MANUAL_REVIEW_REQUIRED",
        "EXPLICIT_PLANNING_REFUSAL",
    }
)


@dataclass(frozen=True)
class RefusalAssessment:
    refused: bool
    value: str | None = None
    decision_date: str | None = None


def normalize_planning_decision(value: Any) -> str:
    if not isinstance(value, (str, int, float)):
        return ""
    return re.sub(r"[^A-Z0-9]+", " ", str(value).upper()).strip()


def validate_triage_bucket(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    if normalized not in TRIAGE_BUCKETS:
        raise ValueError("invalid_triage_bucket")
    return normalized


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def planning_refusal_assessment(metadata: Any) -> RefusalAssessment:
    data = metadata if isinstance(metadata, dict) else {}
    values = (
        data.get("decision"),
        data.get("planning_status"),
        data.get("status"),
        _nested(data, "provider_record", "decision", "outcome"),
        _nested(data, "provider_record", "status"),
    )
    for value in values:
        normalized = normalize_planning_decision(value)
        if normalized in REFUSED_DECISIONS:
            decision_date = data.get("decision_date") or _nested(
                data, "provider_record", "date_decided"
            )
            return RefusalAssessment(True, normalized, str(decision_date or "") or None)
    return RefusalAssessment(False)


def deterministic_review_recommendation(extracted_facts: Any) -> str:
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    if facts.get("likely_false_positive") is True:
        return "REJECT"
    if facts.get("planning_candidate_matched") is False:
        return "REJECT"
    action = str(facts.get("opportunity_creation_decision") or "")
    if facts.get("planning_candidate_matched") is True and action in {
        "CREATE_OPPORTUNITY",
        "SUPPORT_EXISTING_ONLY",
    }:
        return "APPROVE"
    return "NEEDS_HUMAN"


def review_triage_bucket(
    *,
    source_type: str,
    review_status: str,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
    threshold: float = SAFE_APPROVAL_MIN_CONFIDENCE,
) -> str:
    if source_type == "planning" and planning_refusal_assessment(metadata).refused:
        return "EXPLICIT_PLANNING_REFUSAL"
    if review_status != "PENDING" or source_type != "planning":
        return "MANUAL_REVIEW_REQUIRED"
    deterministic = deterministic_review_recommendation(extracted_facts)
    if ai_status != "SUCCEEDED" or ai_recommendation in {None, "NEEDS_HUMAN"}:
        return "AI_UNCERTAIN" if ai_status == "SUCCEEDED" else "MANUAL_REVIEW_REQUIRED"
    if deterministic != ai_recommendation:
        return "DETERMINISTIC_AI_DISAGREE"
    if (
        deterministic == "APPROVE"
        and ai_recommendation == "APPROVE"
        and ai_confidence is not None
        and ai_confidence >= threshold
    ):
        return "SAFE_APPROVE_AGREEMENT"
    return "MANUAL_REVIEW_REQUIRED"


def safe_approval_candidate(
    *,
    source_type: str,
    review_status: str,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
    threshold: float = SAFE_APPROVAL_MIN_CONFIDENCE,
) -> bool:
    return (
        review_triage_bucket(
            source_type=source_type,
            review_status=review_status,
            metadata=metadata,
            extracted_facts=extracted_facts,
            ai_status=ai_status,
            ai_recommendation=ai_recommendation,
            ai_confidence=ai_confidence,
            threshold=threshold,
        )
        == "SAFE_APPROVE_AGREEMENT"
    )
