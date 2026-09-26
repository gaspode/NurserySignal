from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CorrelationMatch:
    matched: bool
    reason: str


@dataclass(frozen=True)
class MatchDecision:
    outcome: str
    confidence: float
    reason: str


def normalize_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def compatible_names(left: Any, right: Any) -> bool:
    ignored = {"nursery", "day", "the", "ltd", "limited", "childcare"}
    a = set(normalize_identity(left).split()) - ignored
    b = set(normalize_identity(right).split()) - ignored
    return bool(a and b and a & b)


def deterministic_match(
    postcode: Any, name: Any, other_postcode: Any, other_name: Any
) -> CorrelationMatch:
    if not postcode or not other_postcode:
        return CorrelationMatch(False, "postcode missing")
    if normalize_identity(postcode) != normalize_identity(other_postcode):
        return CorrelationMatch(False, "postcode differs")
    if not compatible_names(name, other_name):
        return CorrelationMatch(False, "postcode matches but operator/nursery name is incompatible")
    return CorrelationMatch(True, "same postcode and compatible operator/nursery name")


def classify_match(postcode: Any, name: Any, other_postcode: Any, other_name: Any) -> MatchDecision:
    """Return an explainable, conservative generic matching outcome."""
    if (
        postcode
        and other_postcode
        and normalize_identity(postcode) == normalize_identity(other_postcode)
    ):
        if compatible_names(name, other_name):
            return MatchDecision(
                "EXACT", 0.98, "same postcode and compatible operator/nursery name"
            )
        return MatchDecision(
            "UNCERTAIN", 0.55, "same postcode but operator/nursery name is incompatible"
        )
    if not postcode or not other_postcode:
        return MatchDecision("NO_MATCH", 0.0, "postcode missing")
    return MatchDecision("NO_MATCH", 0.0, "postcode differs")


def recruitment_evidence_strength(candidate: dict[str, Any]) -> tuple[float, str]:
    """Return a bounded confidence contribution for recruitment evidence."""
    facts = candidate.get("extracted_facts") or {}
    relevance = facts.get("recruitment_relevance")
    change = facts.get("commercial_change_evidence")
    if relevance == "RELEVANT_CHANGE" or change == "STRONG":
        return 0.18, "strong recruitment change evidence"
    if relevance == "RELEVANT_ROUTINE":
        return 0.05, "routine recruitment corroboration"
    return 0.0, "recruitment evidence is uncertain or not relevant"
