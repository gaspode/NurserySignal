from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.care_planning_review import CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS
from app.planning_outcomes import canonical_planning_outcome, normalize_structured_planning_value

REFUSAL_POLICY_VERSION = "planning-refusal-v4"
SAFE_APPROVAL_MIN_CONFIDENCE = 0.95
SAFE_APPROVAL_POLICY_VERSION = "safe-approval-v1"
SAFE_APPROVAL_VERTICALS = frozenset({"NURSERY"})
SAFE_APPROVAL_QA_MODULUS = 10
TRIAGE_BUCKETS = frozenset(
    {
        "SAFE_APPROVE_AGREEMENT",
        "AUTO_APPROVED_SAFE_AGREEMENT",
        "QA_HOLDOUT_SAFE_AGREEMENT",
        "DETERMINISTIC_AI_DISAGREE",
        "AI_UNCERTAIN",
        "MANUAL_REVIEW_REQUIRED",
        "EXPLICIT_PLANNING_REFUSAL",
        "AUTO_APPROVED_EXPLICIT_NEW_HOME",
        "QA_HOLDOUT_EXPLICIT_NEW_HOME",
        "AUTO_APPROVED_CARE_AI_APPROVAL",
        "QA_HOLDOUT_CARE_AI_APPROVAL",
    }
)


@dataclass(frozen=True)
class RefusalAssessment:
    refused: bool
    value: str | None = None
    decision_date: str | None = None


def normalize_planning_decision(value: Any) -> str:
    return normalize_structured_planning_value(value)


def validate_triage_bucket(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    if normalized not in TRIAGE_BUCKETS:
        raise ValueError("invalid_triage_bucket")
    return normalized


def planning_refusal_assessment(metadata: Any) -> RefusalAssessment:
    outcome = canonical_planning_outcome(metadata)
    return RefusalAssessment(
        outcome.refused,
        outcome.matched_value if outcome.refused else None,
        outcome.decision_date if outcome.refused else None,
    )


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
    signal_id: str | None = None,
    vertical: str = "NURSERY",
    source_type: str,
    review_status: str,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
    threshold: float = SAFE_APPROVAL_MIN_CONFIDENCE,
) -> str:
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    if source_type == "planning" and planning_refusal_assessment(metadata).refused:
        return "EXPLICIT_PLANNING_REFUSAL"
    policy_record = facts.get("safe_approval")
    care_fastpath = facts.get("care_planning_fastpath")
    care_ai_approval = facts.get("care_planning_ai_approval")
    if (
        isinstance(care_ai_approval, dict)
        and care_ai_approval.get("policy_version") in CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS
    ):
        if review_status == "APPROVED" and care_ai_approval.get("outcome") == "AUTO_APPROVE":
            return "AUTO_APPROVED_CARE_AI_APPROVAL"
        if review_status == "PENDING" and care_ai_approval.get("outcome") == "QA_HOLDOUT":
            return "QA_HOLDOUT_CARE_AI_APPROVAL"
    if isinstance(care_fastpath, dict):
        if review_status == "APPROVED" and care_fastpath.get("outcome") == "AUTO_APPROVE":
            return "AUTO_APPROVED_EXPLICIT_NEW_HOME"
        if review_status == "PENDING" and care_fastpath.get("outcome") == "QA_HOLDOUT":
            return "QA_HOLDOUT_EXPLICIT_NEW_HOME"
    if isinstance(policy_record, dict):
        if review_status == "APPROVED" and policy_record.get("outcome") == "AUTO_APPROVE":
            return "AUTO_APPROVED_SAFE_AGREEMENT"
        if review_status == "PENDING" and policy_record.get("outcome") == "QA_HOLDOUT":
            return "QA_HOLDOUT_SAFE_AGREEMENT"
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
        if vertical not in SAFE_APPROVAL_VERTICALS:
            return "MANUAL_REVIEW_REQUIRED"
        if signal_id and safe_approval_qa_holdout(signal_id):
            return "QA_HOLDOUT_SAFE_AGREEMENT"
        return "SAFE_APPROVE_AGREEMENT"
    return "MANUAL_REVIEW_REQUIRED"


def safe_approval_candidate(
    *,
    signal_id: str | None = None,
    vertical: str = "NURSERY",
    source_type: str,
    review_status: str,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
    threshold: float = SAFE_APPROVAL_MIN_CONFIDENCE,
) -> bool:
    return review_triage_bucket(
        signal_id=signal_id,
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        metadata=metadata,
        extracted_facts=extracted_facts,
        ai_status=ai_status,
        ai_recommendation=ai_recommendation,
        ai_confidence=ai_confidence,
        threshold=threshold,
    ) in {"SAFE_APPROVE_AGREEMENT", "QA_HOLDOUT_SAFE_AGREEMENT"}


def safe_approval_qa_bucket(signal_id: str) -> int:
    """Return a stable 0-9 QA bucket derived from the immutable signal UUID."""
    return UUID(str(signal_id)).int % SAFE_APPROVAL_QA_MODULUS


def safe_approval_qa_holdout(signal_id: str) -> bool:
    return safe_approval_qa_bucket(signal_id) == 0


def safe_approval_policy_outcome(
    *,
    signal_id: str,
    vertical: str,
    source_type: str,
    review_status: str,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str:
    """Evaluate the exact v1 policy shared by live processing and backlog tooling."""
    if vertical not in SAFE_APPROVAL_VERTICALS:
        return "INELIGIBLE"
    if not safe_approval_candidate(
        signal_id=signal_id,
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        metadata=metadata,
        extracted_facts=extracted_facts,
        ai_status=ai_status,
        ai_recommendation=ai_recommendation,
        ai_confidence=ai_confidence,
        threshold=SAFE_APPROVAL_MIN_CONFIDENCE,
    ):
        return "INELIGIBLE"
    return "QA_HOLDOUT" if safe_approval_qa_holdout(signal_id) else "AUTO_APPROVE"
