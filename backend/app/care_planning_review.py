from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.planning_outcomes import (
    PlanningOutcome,
    canonical_planning_outcome,
)

CARE_PLANNING_FASTPATH_POLICY_VERSION = "care-planning-fastpath-v1"
CARE_PLANNING_QA_MODULUS = 10
CARE_PLANNING_AI_APPROVAL_POLICY_VERSION = "care-planning-ai-approval-v1.1"
CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS = frozenset(
    {"care-planning-ai-approval-v1", CARE_PLANNING_AI_APPROVAL_POLICY_VERSION}
)
CARE_PLANNING_AI_APPROVAL_PROMPT_VERSION = "care-planning-shadow-v2"
CARE_PLANNING_AI_APPROVAL_MIN_CONFIDENCE = 0.95
CARE_PLANNING_AI_APPROVAL_QA_MODULUS = 20
CARE_PLANNING_LAWFULNESS_POLICY_VERSION = "care-planning-lawfulness-proposed-v1"
CARE_PLANNING_LAWFULNESS_PROMPT_VERSION = "care-planning-shadow-v2"
CARE_PLANNING_LAWFULNESS_MIN_CONFIDENCE = 0.95
CARE_PLANNING_LAWFULNESS_QA_MODULUS = 10
CARE_PLANNING_TAXONOMY_VERSION = "care-planning-taxonomy-v2"
WITHDRAWAL_POLICY_VERSION = "planning-withdrawal-v2"

CARE_PLANNING_AI_APPROVAL_SUBTYPES = frozenset(
    {"NEW_HOME_CHANGE_OF_USE", "NEW_HOME_OTHER_EXPLICIT"}
)
CARE_PLANNING_AI_APPROVAL_BLOCKED_OUTCOMES = frozenset(
    {
        PlanningOutcome.REFUSED,
        PlanningOutcome.WITHDRAWN,
        PlanningOutcome.REFUSED_UNDER_APPEAL,
        PlanningOutcome.APPEAL_DISMISSED,
    }
)

CARE_PLANNING_SUBTYPES = frozenset(
    {
        "NEW_HOME_CHANGE_OF_USE",
        "NEW_HOME_OTHER_EXPLICIT",
        "NEW_HOME_MIXED_USE",
        "LAWFULNESS_PROPOSED",
        "LAWFULNESS_EXISTING",
        "EXPANSION_OR_CAPACITY_CHANGE",
        "CONDITION_VARIATION",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
        "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
        "REFUSED",
        "WITHDRAWN",
        "AMBIGUOUS",
    }
)
CARE_PLANNING_FILTERS = CARE_PLANNING_SUBTYPES | {"EXPLICIT_NEW_HOME"}

_HOME = re.compile(
    r"\b(?:children(?:['’]s|s)?\s+(?:residential\s+)?(?:care\s+)?home|"
    r"residential\s+children(?:['’]s|s)?\s+home|children(?:['’]s|s)?\s+care\s+home|"
    r"children(?:\s*/\s*young\s+persons?|\s+and\s+young\s+people)(?:['’]s)?\s+(?:care\s+)?home|"
    r"residential\s+(?:care\s+)?home\s+for\s+(?:up\s+to\s+\w+\s+)?children|"
    r"residential\s+care\s+home.{0,50}\bchildren\b|"
    r"care\s+home.{0,50}\b(?:children|young\s+people|young\s+persons?)\b)\b",
    re.IGNORECASE,
)
_CHILD_CONTEXT = re.compile(
    r"\b(?:children|child|young\s+people|young\s+persons?|under\s*18s?)\b", re.IGNORECASE
)
_CARE_USE_CONTEXT = re.compile(
    r"\b(?:c2|residential\s+(?:care\s+)?home|care\s+home|residential\s+institution)\b",
    re.IGNORECASE,
)
_CHANGE_TO_CARE = re.compile(
    r"\b(?:change\s+of\s+use|conversion|convert(?:ed|ing)?)\b.{0,180}\b(?:from|of)\b"
    r".{0,120}\b(?:c3a?|c4|dwelling\s*house|dwelling|house\s+in\s+multiple\s+occupation|"
    r"hmo|education(?:al)?|school|pru|office|class\s+e|existing\s+(?:building|property|use))\b"
    r".{0,180}\b(?:to|as|into)\b.{0,160}\b(?:c2|children|child|young\s+people|young\s+persons?)\b|"
    r"\b(?:c3a?|c4|dwelling\s*house|dwelling|hmo|educational?|pru|class\s+e)\b"
    r".{0,120}\b(?:to|into)\b.{0,120}\b(?:c2|children|child|young\s+people|young\s+persons?)\b|"
    r"\buse\s+of\b.{0,100}\b(?:c3a?|dwelling\s*house|dwelling|property)\b"
    r".{0,100}\bas\b.{0,100}\b(?:c2|children|child|young\s+people|young\s+persons?)\b|"
    r"\bchange\s+of\s+use\b.{0,160}\bto\b.{0,120}"
    r"\b(?:c2|children|child|young\s+people|young\s+persons?)\b",
    re.IGNORECASE,
)
_CHANGE_AWAY_FROM_CARE = re.compile(
    r"\b(?:change\s+of\s+use|conversion|convert(?:ed|ing)?)\b.{0,120}\bfrom\b"
    r".{0,120}\b(?:c2|children(?:['’]s|s)?\s+(?:care\s+)?home|residential\s+care\s+home)\b"
    r".{0,120}\bto\b.{0,100}\b(?:c3a?|c4|dwelling|hmo|class\s+e|office|education(?:al)?|"
    r"non[- ]care|another\s+use)\b|"
    r"\b(?:cessation|cease|removal)\b.{0,80}\b(?:children(?:['’]s|s)?\s+home|care\s+use|c2)\b",
    re.IGNORECASE,
)
_EXPLICIT_NEW = re.compile(
    r"\b(?:new|proposed|erect(?:ion|ed|ing)?|develop(?:ment|ed|ing)?|"
    r"create|creation|provide|use\s+as|for\s+use\s+as|to\s+use\b.{0,80}\bas)\b",
    re.IGNORECASE,
)
_EXISTING = re.compile(
    r"\b(?:existing|current|continued)\s+use\s+(?:of\s+\w+\s+)?(?:as\s+)?(?:a\s+)?"
    r"(?:residential\s+)?children(?:['’]s|s)?\s+(?:care\s+)?home\b|"
    r"\bcontinued\s+use\b.{0,100}\b(?:children|young\s+people)\b|"
    r"\bexisting\s+(?:residential\s+)?children(?:['’]s|s)?\s+(?:care\s+)?home\b",
    re.IGNORECASE,
)
_LAWFULNESS = re.compile(
    r"\b(?:certificate\s+of\s+lawful(?:ness|\s+use|\s+development)|"
    r"certificate\s+of\s+existing\s+lawful\s+development|"
    r"lawful\s+development\s+certificate|application\s+for\s+(?:a\s+)?lawful\s+development\s+certificate|"
    r"certificate\s+of\s+lawfulness|application\s+under\s+section\s*192)\b",
    re.IGNORECASE,
)
_LAWFULNESS_PROPOSED = re.compile(
    r"\bproposed\b|\bsection\s*192\b|\bproposed\s+(?:development|use)\b", re.IGNORECASE
)
_LAWFULNESS_EXISTING = re.compile(
    r"\bexisting\s+use\b|\bcontinued\s+use\b|\blawful\s+use\s+existing\b|"
    r"\bexisting\s+lawful\s+development\s+certificate\b|"
    r"\bcertificate\s+of\s+existing\s+lawful\s+development\b",
    re.IGNORECASE,
)
_CONDITION_VARIATION = re.compile(
    r"\b(?:variation|vary|removal)\s+of\s+condition\b|\bsection\s*73\b",
    re.IGNORECASE,
)
_CONDITION_DISCHARGE = re.compile(
    r"\b(?:discharge\s+(?:of\s+)?conditions?|approval\s+of\s+details\s+reserved\s+by\s+condition|"
    r"details\s+pursuant\s+to\s+conditions?|compliance\s+with\s+conditions?)\b",
    re.IGNORECASE,
)
_NON_MATERIAL = re.compile(r"\bnon[- ]material\s+amendment\b", re.IGNORECASE)
_FOLLOW_UP = re.compile(
    r"\b(?:amendment|reserved\s+matters|pursuant\s+to|following\s+permission|"
    r"approved\s+plans?|condition\s+number|extension\s+to|alterations?\s+to|"
    r"installation\s+(?:at|to))\b",
    re.IGNORECASE,
)
_MATERIAL_CAPACITY = re.compile(
    r"\b(?:increase|increasing|increased|raise|extend)\b.{0,80}"
    r"\b(?:capacity|occupancy|places?|beds?|children|young\s+people|residents?)\b|"
    r"\b(?:addition|additional)\s+of\s+(?:one|two|three|four|\d+)\s+"
    r"(?:children|young\s+people|young\s+persons?|young\s+person|residents?|beds?|places?)\b|"
    r"\bfrom\s+\d+\s+(?:children|young\s+people|residents?|beds?|places?)\s+to\s+\d+\b",
    re.IGNORECASE,
)
_RETROSPECTIVE = re.compile(
    r"\b(?:retrospective|regularis(?:e|ation)|regulariz(?:e|ation))\b", re.IGNORECASE
)
_MIXED_USE = re.compile(
    r"\b(?:mixed[- ]use|outline\s+application|masterplan|urban\s+extension|"
    r"development\s+comprising|scheme\s+comprising)\b",
    re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"\b(?:planning\s+permission|application|reference|ref(?:erence)?\.?)[\s:#]*"
    r"((?:[A-Z0-9]{1,15}\s*[/._-]\s*)+[A-Z0-9]{1,15}|[A-Z0-9]{5,30})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WithdrawalAssessment:
    withdrawn: bool
    value: str | None = None
    decision_date: str | None = None


@dataclass(frozen=True)
class CarePlanningSubtypeAssessment:
    subtype: str
    explicit_new_home: bool
    material_capacity_change: bool
    prior_application_references: tuple[str, ...]
    reasons: tuple[str, ...]


def planning_withdrawal_assessment(metadata: Any) -> WithdrawalAssessment:
    outcome = canonical_planning_outcome(metadata)
    return WithdrawalAssessment(
        outcome.withdrawn,
        outcome.matched_value if outcome.withdrawn else None,
        outcome.decision_date if outcome.withdrawn else None,
    )


def care_planning_decision_allows_fastpath(metadata: Any) -> bool:
    return canonical_planning_outcome(metadata).outcome in {
        PlanningOutcome.APPROVED,
        PlanningOutcome.PENDING,
    }


def validate_care_planning_subtype(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    if normalized not in CARE_PLANNING_FILTERS:
        raise ValueError("invalid_planning_subtype")
    return normalized


def _planning_text(raw: dict[str, Any]) -> str:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    provider = metadata.get("provider_record")
    provider = provider if isinstance(provider, dict) else {}
    values = (
        raw.get("title"),
        raw.get("raw_text"),
        provider.get("description"),
        metadata.get("description"),
        provider.get("proposal"),
        provider.get("application_type"),
        provider.get("planning_route"),
    )
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        text = text.replace("", "'").replace("’", "'")
        text = re.sub(r"\bchildren\?s\b", "children's", text, flags=re.IGNORECASE)
        if text and text not in unique:
            unique.append(text)
    # Keep source fields on separate lines so bounded direction regexes cannot
    # accidentally join the target use in one representation to the source
    # use in a repeated title/raw/provider representation.
    return "\n".join(unique)


def extract_prior_planning_references(text: str) -> tuple[str, ...]:
    references: list[str] = []
    for match in _REFERENCE.finditer(text):
        reference = match.group(1).strip(".,;:()[]{}").upper()
        # UK planning references contain a numeric component. This prevents
        # prose such as "application submitted under Section 73" from
        # producing a malformed SUBMITTED alias/reference.
        if not any(character.isdigit() for character in reference):
            continue
        if reference not in references:
            references.append(reference)
    return tuple(references[:10])


def classify_care_planning_subtype(raw: dict[str, Any]) -> CarePlanningSubtypeAssessment:
    text = _planning_text(raw)
    metadata = raw.get("metadata")
    references = extract_prior_planning_references(text)
    outcome = canonical_planning_outcome(metadata)
    if outcome.refused:
        return CarePlanningSubtypeAssessment(
            "REFUSED", False, False, references, ("explicit structured refusal",)
        )
    if outcome.withdrawn:
        return CarePlanningSubtypeAssessment(
            "WITHDRAWN", False, False, references, ("explicit structured withdrawal",)
        )
    if outcome.outcome is PlanningOutcome.REFUSED_UNDER_APPEAL:
        return CarePlanningSubtypeAssessment(
            "FOLLOW_UP_OTHER",
            False,
            False,
            references,
            ("refused application under active appeal",),
        )
    if outcome.outcome is PlanningOutcome.APPEAL_ALLOWED:
        return CarePlanningSubtypeAssessment(
            "FOLLOW_UP_OTHER",
            False,
            False,
            references,
            ("appeal allowed lifecycle evidence",),
        )
    home = bool(_HOME.search(text))
    child_context = bool(_CHILD_CONTEXT.search(text))
    care_context = bool(_CARE_USE_CONTEXT.search(text))
    material_capacity = bool(_MATERIAL_CAPACITY.search(text))
    # Current procedural purpose wins over quoted text describing the original
    # permission. These checks deliberately precede all new-home semantics.
    if _NON_MATERIAL.search(text):
        return CarePlanningSubtypeAssessment(
            "NON_MATERIAL_AMENDMENT",
            False,
            material_capacity,
            references,
            ("non-material amendment wording",),
        )
    if _CONDITION_DISCHARGE.search(text):
        return CarePlanningSubtypeAssessment(
            "CONDITION_DISCHARGE",
            False,
            material_capacity,
            references,
            ("condition discharge/details wording",),
        )
    if _CONDITION_VARIATION.search(text):
        reasons = ["condition variation wording"]
        if material_capacity:
            reasons.append("material capacity wording")
        return CarePlanningSubtypeAssessment(
            "CONDITION_VARIATION", False, material_capacity, references, tuple(reasons)
        )
    if _CHANGE_AWAY_FROM_CARE.search(text):
        return CarePlanningSubtypeAssessment(
            "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
            False,
            False,
            references,
            ("direction of use change is away from children-care provision",),
        )
    if _LAWFULNESS.search(text):
        proposed = bool(_LAWFULNESS_PROPOSED.search(text))
        existing = bool(_LAWFULNESS_EXISTING.search(text))
        if existing and home:
            return CarePlanningSubtypeAssessment(
                "LAWFULNESS_EXISTING",
                False,
                False,
                references,
                ("existing-use lawfulness route", "explicit children-home wording"),
            )
        if not existing and home and (proposed or _CHANGE_TO_CARE.search(text)):
            return CarePlanningSubtypeAssessment(
                "LAWFULNESS_PROPOSED",
                True,
                False,
                references,
                ("proposed lawfulness route", "explicit children-home wording"),
            )
        return CarePlanningSubtypeAssessment(
            "AMBIGUOUS", False, False, references, ("ambiguous lawfulness wording",)
        )
    if home and _MIXED_USE.search(text):
        return CarePlanningSubtypeAssessment(
            "NEW_HOME_MIXED_USE",
            True,
            False,
            references,
            ("explicit children-home component in mixed-use development",),
        )
    if home and material_capacity and (_EXISTING.search(text) or _RETROSPECTIVE.search(text)):
        return CarePlanningSubtypeAssessment(
            "EXPANSION_OR_CAPACITY_CHANGE",
            False,
            True,
            references,
            ("existing children-home use", "material capacity-change wording"),
        )
    if _FOLLOW_UP.search(text):
        return CarePlanningSubtypeAssessment(
            "FOLLOW_UP_OTHER", False, material_capacity, references, ("follow-up planning wording",)
        )
    if home and (_EXISTING.search(text) or _RETROSPECTIVE.search(text)):
        return CarePlanningSubtypeAssessment(
            "FOLLOW_UP_OTHER",
            False,
            material_capacity,
            references,
            ("existing children-home use wording",),
        )
    if home and _CHANGE_TO_CARE.search(text):
        return CarePlanningSubtypeAssessment(
            "NEW_HOME_CHANGE_OF_USE",
            True,
            False,
            references,
            ("explicit change of use", "explicit children-home wording"),
        )
    if home and _EXPLICIT_NEW.search(text):
        return CarePlanningSubtypeAssessment(
            "NEW_HOME_OTHER_EXPLICIT", True, False, references, ("explicit children-home proposal",)
        )
    if child_context and care_context:
        return CarePlanningSubtypeAssessment(
            "AMBIGUOUS",
            False,
            material_capacity,
            references,
            ("child and care context present without clear current application direction",),
        )
    return CarePlanningSubtypeAssessment(
        "AMBIGUOUS", False, material_capacity, references, ("insufficient planning semantics",)
    )


def care_planning_fastpath_qa_bucket(signal_id: str) -> int:
    return UUID(str(signal_id)).int % CARE_PLANNING_QA_MODULUS


def care_planning_fastpath_qa_holdout(signal_id: str) -> bool:
    return care_planning_fastpath_qa_bucket(signal_id) == 0


def care_planning_ai_approval_qa_bucket(signal_id: str) -> int:
    """Return the stable v1.1 0-19 QA bucket for the immutable signal UUID."""
    return UUID(str(signal_id)).int % CARE_PLANNING_AI_APPROVAL_QA_MODULUS


def care_planning_ai_approval_qa_holdout(signal_id: str) -> bool:
    return care_planning_ai_approval_qa_bucket(signal_id) == 0


def care_planning_ai_approval_exclusion(
    *,
    vertical: str,
    source_type: str,
    review_status: str,
    reviewed_by: str | None,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_prompt_version: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str | None:
    """Return one mutually exclusive reason why Care AI approval v1 cannot apply."""
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    if vertical != "CHILDRENS_HOME" or source_type != "planning":
        return "OUT_OF_SCOPE"
    if review_status != "PENDING" or reviewed_by:
        return "EXISTING_REVIEW_OR_POLICY_STATE"
    if any(
        isinstance(facts.get(marker), dict)
        for marker in (
            "automatic_review",
            "safe_approval",
            "care_planning_fastpath",
            "care_planning_ai_approval",
            "care_planning_lawfulness_approval",
        )
    ):
        return "EXISTING_REVIEW_OR_POLICY_STATE"
    outcome = canonical_planning_outcome(metadata).outcome
    if outcome in CARE_PLANNING_AI_APPROVAL_BLOCKED_OUTCOMES:
        return "PLANNING_OUTCOME"
    subtype = str(facts.get("planning_subtype") or "AMBIGUOUS")
    if subtype not in CARE_PLANNING_AI_APPROVAL_SUBTYPES:
        return "SUBTYPE"
    if facts.get("likely_false_positive") is True or facts.get("planning_ambiguity_markers"):
        return "AMBIGUITY_OR_FALSE_POSITIVE"
    if ai_status != "SUCCEEDED" or (ai_prompt_version != CARE_PLANNING_AI_APPROVAL_PROMPT_VERSION):
        return "AI_VERSION_OR_STATUS"
    if ai_recommendation != "APPROVE":
        return "AI_RECOMMENDATION"
    if ai_confidence is None or ai_confidence < CARE_PLANNING_AI_APPROVAL_MIN_CONFIDENCE:
        return "AI_CONFIDENCE"
    return None


def care_planning_ai_approval_outcome(
    *,
    signal_id: str,
    vertical: str,
    source_type: str,
    review_status: str,
    reviewed_by: str | None,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_prompt_version: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str:
    exclusion = care_planning_ai_approval_exclusion(
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        reviewed_by=reviewed_by,
        metadata=metadata,
        extracted_facts=extracted_facts,
        ai_status=ai_status,
        ai_prompt_version=ai_prompt_version,
        ai_recommendation=ai_recommendation,
        ai_confidence=ai_confidence,
    )
    if exclusion:
        return "NOT_ELIGIBLE"
    return "QA_HOLDOUT" if care_planning_ai_approval_qa_holdout(signal_id) else "AUTO_APPROVE"


def care_planning_lawfulness_qa_bucket(signal_id: str) -> int:
    """Return the stable 0-9 QA bucket for the immutable signal UUID."""
    return UUID(str(signal_id)).int % CARE_PLANNING_LAWFULNESS_QA_MODULUS


def care_planning_lawfulness_qa_holdout(signal_id: str) -> bool:
    return care_planning_lawfulness_qa_bucket(signal_id) == 0


def care_planning_lawfulness_exclusion(
    *,
    vertical: str,
    source_type: str,
    review_status: str,
    reviewed_by: str | None,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_prompt_version: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str | None:
    """Return one reason why proposed-lawfulness approval v1 cannot apply."""
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    if vertical != "CHILDRENS_HOME" or source_type != "planning":
        return "OUT_OF_SCOPE"
    if review_status != "PENDING" or reviewed_by:
        return "EXISTING_REVIEW_OR_POLICY_STATE"
    if any(
        isinstance(facts.get(marker), dict)
        for marker in (
            "automatic_review",
            "safe_approval",
            "care_planning_fastpath",
            "care_planning_ai_approval",
            "care_planning_lawfulness_approval",
        )
    ):
        return "EXISTING_REVIEW_OR_POLICY_STATE"
    if str(facts.get("planning_subtype") or "") != "LAWFULNESS_PROPOSED":
        return "SUBTYPE"
    if canonical_planning_outcome(metadata).outcome is not PlanningOutcome.APPROVED:
        return "PLANNING_OUTCOME"
    if facts.get("opportunity_creation_decision") != "CREATE_OPPORTUNITY":
        return "OPPORTUNITY_DECISION"
    if facts.get("explicit_new_home_proposal") is not True:
        return "NO_EXPLICIT_NEW_HOME_WORDING"
    if facts.get("likely_false_positive") is True:
        return "LIKELY_FALSE_POSITIVE"
    if facts.get("planning_ambiguity_markers"):
        return "AMBIGUITY"
    if facts.get("planning_prior_references"):
        return "PRIOR_APPLICATION_REFERENCE"
    if ai_status != "SUCCEEDED" or ai_prompt_version != CARE_PLANNING_LAWFULNESS_PROMPT_VERSION:
        return "AI_VERSION_OR_STATUS"
    if ai_recommendation != "APPROVE":
        return "AI_RECOMMENDATION"
    if ai_confidence is None or ai_confidence < CARE_PLANNING_LAWFULNESS_MIN_CONFIDENCE:
        return "AI_CONFIDENCE"
    return None


def care_planning_lawfulness_outcome(
    *,
    signal_id: str,
    vertical: str,
    source_type: str,
    review_status: str,
    reviewed_by: str | None,
    metadata: Any,
    extracted_facts: Any,
    ai_status: str | None,
    ai_prompt_version: str | None,
    ai_recommendation: str | None,
    ai_confidence: float | None,
) -> str:
    exclusion = care_planning_lawfulness_exclusion(
        vertical=vertical,
        source_type=source_type,
        review_status=review_status,
        reviewed_by=reviewed_by,
        metadata=metadata,
        extracted_facts=extracted_facts,
        ai_status=ai_status,
        ai_prompt_version=ai_prompt_version,
        ai_recommendation=ai_recommendation,
        ai_confidence=ai_confidence,
    )
    if exclusion:
        return "NOT_ELIGIBLE"
    return "QA_HOLDOUT" if care_planning_lawfulness_qa_holdout(signal_id) else "AUTO_APPROVE"


def care_planning_fastpath_eligible(
    *, vertical: str, source_type: str, review_status: str, extracted_facts: Any
) -> bool:
    facts = extracted_facts if isinstance(extracted_facts, dict) else {}
    return bool(
        vertical == "CHILDRENS_HOME"
        and source_type == "planning"
        and review_status == "PENDING"
        and facts.get("planning_subtype") in {"NEW_HOME_CHANGE_OF_USE", "NEW_HOME_OTHER_EXPLICIT"}
        and facts.get("planning_candidate_matched") is True
        and facts.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY"
        and facts.get("explicit_new_home_proposal") is True
        and facts.get("planning_decision_fastpath_eligible") is True
        and facts.get("likely_false_positive") is not True
        and not facts.get("planning_ambiguity_markers")
    )
