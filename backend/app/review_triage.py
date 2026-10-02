from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.care_planning_review import (
    CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS,
    CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
)
from app.planning_outcomes import canonical_planning_outcome, normalize_structured_planning_value

REFUSAL_POLICY_VERSION = "planning-refusal-v4"
SAFE_APPROVAL_MIN_CONFIDENCE = 0.95
SAFE_APPROVAL_POLICY_VERSION = "safe-approval-v1"
SAFE_APPROVAL_VERTICALS = frozenset({"NURSERY"})
SAFE_APPROVAL_QA_MODULUS = 10
ROUTINE_RECRUITMENT_POLICY_VERSION = "nursery-routine-recruitment-v1"
ROUTINE_RECRUITMENT_QA_MODULUS = 10
NURSERY_PLANNING_LOSS_POLICY_VERSION = "nursery-planning-loss-v2"
NURSERY_PLANNING_LOSS_QA_MODULUS = 10
NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION = "nursery-planning-arboriculture-v1"
NURSERY_PLANNING_ARBORICULTURE_QA_MODULUS = 10
NURSERY_PLANNING_EXTENSION_POLICY_VERSION = "nursery-planning-extension-v2"
NURSERY_PLANNING_EXTENSION_QA_MODULUS = 10

_EXPLICIT_NURSERY_LOSS_RE = re.compile(
    # The source use must itself be the nursery. In particular, do not let a
    # later "flat roof" make "residential property to nursery" look like a
    # nursery-to-residential conversion.
    r"\b(?:change\s+of\s+use|conversion)\s+(?:"
    r"from\s+[^.]{0,90}?\b(?:day\s+)?(?:pre[ -]?school|nursery)\b"
    r"|of\s+(?:(?!\bto\b)[^.]){0,90}?\b(?:day\s+)?(?:pre[ -]?school|nursery)\b"
    r")\s*[^.]{0,90}?\b(?:to|into)\b[^.]{0,90}?"
    r"\b(?:residential|dwelling(?:house)?|flat(?:s)?|hmo|class\s*c3)\b",
    re.IGNORECASE,
)

_NURSERY_LOSS_FOLLOW_UP_RE = re.compile(
    r"\b(?:condition|variation|pursuant|details|non[- ]material|amendment)\b", re.IGNORECASE
)

_NURSERY_ARBORICULTURE_RE = re.compile(
    r"\b(?:tree(?:s)?|arboricultural|arborist|tree\s+surgeon|"
    r"crown\s+(?:lift|reduce)|pollard|fell|prun(?:e|ing)|canopy)\b",
    re.IGNORECASE,
)
_NURSERY_ARBORICULTURE_EXCLUSION_RE = re.compile(
    r"\b(?:condition|variation|pursuant|details|non[- ]material|amendment|"
    r"change\s+of\s+use|conversion|demolition|erection|extension|"
    r"new\s+(?:nursery|building))\b",
    re.IGNORECASE,
)
_NURSERY_EXTENSION_RE = re.compile(
    r"\b(?:extension|extend|enlargement)\b.{0,120}\b(?:to|of|for)\b.{0,80}"
    r"\b(?:existing\s+)?(?:day\s+)?(?:children['’]s\s+)?(?:pre[ -]?school|nursery)\b",
    re.IGNORECASE,
)
_NURSERY_EXTENSION_EXCLUSION_RE = re.compile(
    r"\b(?:condition|variation|pursuant|details|non[- ]material|amendment|"
    r"change\s+of\s+use|conversion|school|academy|college|classroom|"
    r"children['’]s\s+home|care\s+home|mixed|flat|demolition|"
    r"not\s+developed|previously\s+approved)\b",
    re.IGNORECASE,
)
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
        "AUTO_APPROVED_CARE_LAWFULNESS",
        "QA_HOLDOUT_CARE_LAWFULNESS",
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
    care_lawfulness = facts.get("care_planning_lawfulness_approval")
    if (
        isinstance(care_lawfulness, dict)
        and care_lawfulness.get("policy_version") == CARE_PLANNING_LAWFULNESS_POLICY_VERSION
    ):
        if review_status == "APPROVED" and care_lawfulness.get("outcome") == "AUTO_APPROVE":
            return "AUTO_APPROVED_CARE_LAWFULNESS"
        if review_status == "PENDING" and care_lawfulness.get("outcome") == "QA_HOLDOUT":
            return "QA_HOLDOUT_CARE_LAWFULNESS"
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


def routine_recruitment_candidate(
    *,
    vertical: str,
    source_type: str,
    review_status: str,
    extracted_facts: Any,
) -> bool:
    """Return only routine Nursery recruitment already known to support, not create, work.

    This deliberately does not use an AI recommendation.  It is limited to the
    deterministic recruitment-v2 classification that has an explicit
    SUPPORT_EXISTING_ONLY decision and no ambiguity flags.
    """
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    return (
        vertical == "NURSERY"
        and source_type == "recruitment"
        and review_status == "PENDING"
        and facts.get("classification") == "recruitment-routine"
        and facts.get("recruitment_relevance") == "RELEVANT_ROUTINE"
        and facts.get("recruitment_candidate_matched") is True
        and facts.get("commercial_change_evidence") == "NONE"
        and facts.get("opportunity_creation_decision") == "SUPPORT_EXISTING_ONLY"
        and not facts.get("recruitment_ambiguity_flags")
        and not facts.get("recruitment_exclusions")
    )


def routine_recruitment_qa_bucket(signal_id: str) -> int:
    return UUID(str(signal_id)).int % ROUTINE_RECRUITMENT_QA_MODULUS


def routine_recruitment_qa_holdout(signal_id: str) -> bool:
    return routine_recruitment_qa_bucket(signal_id) == 0


def routine_recruitment_policy_outcome(
    *, signal_id: str, vertical: str, source_type: str, review_status: str, extracted_facts: Any
) -> str:
    if not routine_recruitment_candidate(
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        extracted_facts=extracted_facts,
    ):
        return "INELIGIBLE"
    return "QA_HOLDOUT" if routine_recruitment_qa_holdout(signal_id) else "AUTO_APPROVE"


def explicit_nursery_loss_candidate(
    *, vertical: str, source_type: str, review_status: str, title: str | None
) -> bool:
    """Identify a primary Planning application explicitly removing nursery use.

    This is intentionally narrower than generic title matching: follow-up
    records and new/mixed nursery proposals remain for human review.
    """
    text = str(title or "")
    return (
        vertical == "NURSERY"
        and source_type == "planning"
        and review_status == "PENDING"
        and bool(_EXPLICIT_NURSERY_LOSS_RE.search(text))
        and not _NURSERY_LOSS_FOLLOW_UP_RE.search(text)
    )


def explicit_nursery_loss_qa_holdout(signal_id: str) -> bool:
    return UUID(str(signal_id)).int % NURSERY_PLANNING_LOSS_QA_MODULUS == 0


def explicit_nursery_loss_policy_outcome(
    *, signal_id: str, vertical: str, source_type: str, review_status: str, title: str | None
) -> str:
    if not explicit_nursery_loss_candidate(
        vertical=vertical, source_type=source_type, review_status=review_status, title=title
    ):
        return "INELIGIBLE"
    return "QA_HOLDOUT" if explicit_nursery_loss_qa_holdout(signal_id) else "AUTO_REJECT"


def nursery_arboriculture_disagreement_candidate(
    *,
    vertical: str,
    source_type: str,
    review_status: str,
    title: str | None,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> bool:
    """Recognise pure arboricultural works misclassified by nursery wording.

    This intentionally leaves proposals which could alter nursery capacity or
    use (including extensions, construction and procedural applications) with
    human review. The AI disagreement is a corroborating guard, not the
    source of the semantic decision.
    """
    text = str(title or "")
    return (
        vertical == "NURSERY"
        and source_type == "planning"
        and review_status == "PENDING"
        and deterministic_review_recommendation(extracted_facts) == "APPROVE"
        and ai_status == "SUCCEEDED"
        and ai_recommendation == "REJECT"
        and ai_confidence is not None
        and float(ai_confidence) >= 0.8
        and bool(_NURSERY_ARBORICULTURE_RE.search(text[:180]))
        and not _NURSERY_ARBORICULTURE_EXCLUSION_RE.search(text)
    )


def nursery_arboriculture_disagreement_qa_holdout(signal_id: str) -> bool:
    return UUID(str(signal_id)).int % NURSERY_PLANNING_ARBORICULTURE_QA_MODULUS == 0


def nursery_arboriculture_disagreement_policy_outcome(
    *,
    signal_id: str,
    vertical: str,
    source_type: str,
    review_status: str,
    title: str | None,
    extracted_facts: Any,
    ai_status: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str:
    if not nursery_arboriculture_disagreement_candidate(
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        title=title,
        extracted_facts=extracted_facts,
        ai_status=ai_status,
        ai_recommendation=ai_recommendation,
        ai_confidence=ai_confidence,
    ):
        return "INELIGIBLE"
    return (
        "QA_HOLDOUT" if nursery_arboriculture_disagreement_qa_holdout(signal_id) else "AUTO_REJECT"
    )


def nursery_extension_candidate(
    *, vertical: str, source_type: str, review_status: str, title: str | None
) -> bool:
    """Recognise a standalone, explicit extension to an existing nursery.

    This does not cover a new nursery, a conversion, a school scheme, or a
    procedural record. Those remain for reviewer judgement.
    """
    text = str(title or "")
    return (
        vertical == "NURSERY"
        and source_type == "planning"
        and review_status == "PENDING"
        and bool(_NURSERY_EXTENSION_RE.search(text))
        and not _NURSERY_EXTENSION_EXCLUSION_RE.search(text)
    )


def nursery_extension_qa_holdout(signal_id: str) -> bool:
    return UUID(str(signal_id)).int % NURSERY_PLANNING_EXTENSION_QA_MODULUS == 0


def nursery_extension_policy_outcome(
    *, signal_id: str, vertical: str, source_type: str, review_status: str, title: str | None
) -> str:
    if not nursery_extension_candidate(
        vertical=vertical, source_type=source_type, review_status=review_status, title=title
    ):
        return "INELIGIBLE"
    return "QA_HOLDOUT" if nursery_extension_qa_holdout(signal_id) else "AUTO_APPROVE"
