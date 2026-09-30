from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.planning_outcomes import PlanningOutcome, canonical_planning_outcome


class EvidenceSupport(StrEnum):
    FOUNDATIONAL = "FOUNDATIONAL"
    SUPPORTING_FOLLOWUP = "SUPPORTING_FOLLOWUP"
    OTHER_LIFECYCLE = "OTHER_LIFECYCLE"
    NON_SUPPORTING = "NON_SUPPORTING"


NEGATIVE_PLANNING_OUTCOMES = frozenset(
    {
        PlanningOutcome.REFUSED,
        PlanningOutcome.WITHDRAWN,
        PlanningOutcome.REFUSED_UNDER_APPEAL,
        PlanningOutcome.APPEAL_DISMISSED,
    }
)

PROCEDURAL_OR_NON_OPENING_SUBTYPES = frozenset(
    {
        "LAWFULNESS_EXISTING",
        "CONDITION_VARIATION",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
        "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
        "REFUSED",
        "WITHDRAWN",
    }
)


def _family_relationship_types(signal: dict[str, Any]) -> set[str]:
    values = signal.get("planning_family_relationship_types") or []
    if not isinstance(values, list):
        values = []
    families = signal.get("planning_families") or []
    if isinstance(families, list):
        values = [
            *values,
            *[
                family.get("relationship_type")
                for family in families
                if isinstance(family, dict)
            ],
        ]
    return {str(value) for value in values if value}


def classify_evidence_support(signal: dict[str, Any]) -> EvidenceSupport:
    """Classify one linked signal using current authoritative review semantics."""
    relationship_status = signal.get("relationship_status", signal.get("status"))
    if relationship_status != "ACTIVE" or signal.get("review_status") != "APPROVED":
        return EvidenceSupport.NON_SUPPORTING

    source_type = str(signal.get("source_type") or "").lower()
    if source_type == "procurement":
        return EvidenceSupport.NON_SUPPORTING
    if source_type != "planning":
        return (
            EvidenceSupport.FOUNDATIONAL
            if source_type in {"recruitment", "ofsted"}
            else EvidenceSupport.SUPPORTING_FOLLOWUP
        )

    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    outcome = canonical_planning_outcome(metadata).outcome
    if outcome in NEGATIVE_PLANNING_OUTCOMES:
        return EvidenceSupport.OTHER_LIFECYCLE

    facts = (
        signal.get("extracted_facts")
        if isinstance(signal.get("extracted_facts"), dict)
        else {}
    )
    subtype = str(facts.get("planning_subtype") or "")
    decision = str(facts.get("opportunity_creation_decision") or "")
    relationship_facts = (
        signal.get("relationship_extracted_facts")
        if isinstance(signal.get("relationship_extracted_facts"), dict)
        else {}
    )
    relationship_decision = str(
        relationship_facts.get("opportunity_creation_decision") or ""
    )
    family_relationships = _family_relationship_types(signal)

    if subtype == "CESSATION_OR_CHANGE_AWAY_FROM_CARE":
        return EvidenceSupport.OTHER_LIFECYCLE
    creates_opportunity = decision == "CREATE_OPPORTUNITY" or (
        decision in {"", "REVIEW"} and relationship_decision == "CREATE_OPPORTUNITY"
    )
    if (
        creates_opportunity
        and subtype not in PROCEDURAL_OR_NON_OPENING_SUBTYPES
        and not (
            "REFERENCES_APPLICATION" in family_relationships
            and "PRIMARY_APPLICATION" not in family_relationships
        )
    ):
        return EvidenceSupport.FOUNDATIONAL
    if decision == "SUPPORT_EXISTING_ONLY" or "REFERENCES_APPLICATION" in family_relationships:
        return EvidenceSupport.SUPPORTING_FOLLOWUP
    return EvidenceSupport.NON_SUPPORTING
