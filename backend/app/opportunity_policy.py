from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OpportunityCreationDecision:
    decision: str
    change_type: str
    reason: str


_MATERIAL_PLANNING_PATTERNS = (
    ("EXPANSION", r"\b(?:expan(?:sion|ding)|increas(?:e|ed|ing)|additional|capacity|places)\b"),
    (
        "OPENING",
        r"\b(?:new|create|creating|conversion|convert|change\s+of\s+use|construct|building|accommodat)\w*\b",
    ),
    ("RELOCATION", r"\b(?:relocat|move\s+to|new\s+premises|new\s+site)\w*\b"),
)
_INCIDENTAL_PLANNING_PATTERN = re.compile(
    r"\b(?:near|nearby|adjacent\s+to|next\s+to|close\s+to|opposite)\b"
    r"[^.]{0,120}\b(?:nurser(?:y|ies)|preschool|childcare|early\s+years)\b",
    re.IGNORECASE,
)


def _text(candidate: dict[str, Any]) -> str:
    facts = candidate.get("extracted_facts") or {}
    return " ".join(
        str(value or "")
        for value in (
            candidate.get("nursery_name"),
            candidate.get("operator_name"),
            candidate.get("address"),
            candidate.get("event_type"),
            facts.get("classification"),
            facts.get("planning_positive_terms"),
            facts.get("recruitment_explicit_change_terms"),
            facts.get("source_type"),
        )
    ).lower()


def _planning_change(candidate: dict[str, Any]) -> tuple[str | None, str | None]:
    facts = candidate.get("extracted_facts") or {}
    if facts.get("planning_candidate_matched") is False:
        return None, "planning candidate was excluded"
    raw_text = str(candidate.get("raw_text") or "")
    text = f"{raw_text} {_text(candidate)}".lower()
    childcare_change_terms = (
        "new nursery",
        "day nursery",
        "nursery provision",
        "nursery accommodation",
        "nursery buildings",
        "nursery places",
        "nursery capacity",
        "pre-school",
        "preschool",
        "childcare facility",
        "early years provision",
    )
    incidental_only = _INCIDENTAL_PLANNING_PATTERN.search(raw_text) and not any(
        term in text for term in childcare_change_terms
    )
    for change_type, pattern in _MATERIAL_PLANNING_PATTERNS:
        if incidental_only and change_type == "OPENING":
            continue
        if re.search(pattern, text, re.IGNORECASE):
            return change_type, f"material planning wording matched {change_type.lower()} evidence"
    # A planning candidate without material wording can support a known
    # opportunity, but should not create a new commercial opportunity.
    return None, "planning signal is relevant but has no material-change evidence"


def opportunity_creation_decision(candidate: dict[str, Any]) -> OpportunityCreationDecision:
    """Apply the generic opportunity-creation gate to one enriched signal."""
    facts = candidate.get("extracted_facts") or {}
    source_type = str(facts.get("source_type") or candidate.get("source_type") or "").lower()
    if facts.get("likely_false_positive") or facts.get("classification") in {
        "horticultural-nursery",
        "recruitment-excluded",
        "planning-excluded",
        "irrelevant-or-unclear",
    }:
        return OpportunityCreationDecision(
            "IGNORE_FOR_OPPORTUNITY", "OTHER_CHANGE", "signal is irrelevant or a false positive"
        )
    if source_type == "recruitment":
        relevance = facts.get("recruitment_relevance")
        change = facts.get("commercial_change_evidence")
        if relevance == "RELEVANT_CHANGE" or change == "STRONG":
            return OpportunityCreationDecision(
                "CREATE_OPPORTUNITY", "OPENING", "recruitment explicitly indicates material change"
            )
        if relevance == "RELEVANT_ROUTINE":
            return OpportunityCreationDecision(
                "SUPPORT_EXISTING_ONLY",
                "OTHER_CHANGE",
                "routine recruitment supports an existing opportunity only",
            )
        if relevance == "UNCERTAIN":
            return OpportunityCreationDecision(
                "REVIEW", "OTHER_CHANGE", "recruitment relevance is ambiguous"
            )
        return OpportunityCreationDecision(
            "IGNORE_FOR_OPPORTUNITY",
            "OTHER_CHANGE",
            "recruitment is not relevant to an early-years setting",
        )
    if source_type == "planning":
        change_type, reason = _planning_change(candidate)
        if change_type:
            return OpportunityCreationDecision("CREATE_OPPORTUNITY", change_type, reason or "")
        if facts.get("planning_candidate_matched"):
            return OpportunityCreationDecision(
                "SUPPORT_EXISTING_ONLY", "OTHER_CHANGE", reason or ""
            )
        return OpportunityCreationDecision("IGNORE_FOR_OPPORTUNITY", "OTHER_CHANGE", reason or "")
    return OpportunityCreationDecision(
        "REVIEW", "OTHER_CHANGE", "source-specific opportunity policy is unavailable"
    )


def opportunity_title(candidate: dict[str, Any], decision: OpportunityCreationDecision) -> str:
    """Build a concise title without inventing an unsupported organisation."""
    operator = str(candidate.get("operator_name") or "").strip()
    address = str(candidate.get("address") or "").strip()
    postcode = str((candidate.get("metadata") or {}).get("postcode") or "").strip()
    location = postcode or address
    if (
        operator
        and len(operator) <= 100
        and operator.lower() not in str(candidate.get("raw_text") or "").lower()
    ):
        subject = operator
    elif address and len(address) <= 100:
        subject = address
    else:
        subject = "Nursery setting"
    labels = {
        "OPENING": "New nursery",
        "EXPANSION": "Nursery capacity expansion",
        "RELOCATION": "Nursery relocation",
    }
    label = labels.get(decision.change_type, "Nursery commercial change")
    return f"{label} — {subject}" + (
        f" ({location})" if location and location.lower() not in subject.lower() else ""
    )
