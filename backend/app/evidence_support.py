from __future__ import annotations

from enum import StrEnum
from typing import Any

from app.planning_outcomes import (
    PLANNING_OUTCOME_POLICY_VERSION,
    PlanningOutcome,
    canonical_planning_outcome,
    structured_planning_fields,
)


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


def planning_timeline_projection(signal: dict[str, Any]) -> dict[str, Any]:
    """Project compact, authoritative Planning semantics for the admin timeline."""
    if str(signal.get("source_type") or "").lower() != "planning":
        return {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    facts = (
        signal.get("extracted_facts")
        if isinstance(signal.get("extracted_facts"), dict)
        else {}
    )
    outcome = canonical_planning_outcome(metadata)
    raw_fields = {
        field: value
        for field, value in structured_planning_fields(metadata)
        if isinstance(value, (str, int, float)) and str(value).strip()
    }
    decision_raw = raw_fields.get("decision") or raw_fields.get(
        "provider_record.decision.outcome"
    )
    status_raw = (
        raw_fields.get("planning_status")
        or raw_fields.get("status")
        or raw_fields.get("provider_record.status")
    )
    support = classify_evidence_support(signal)
    decision = str(facts.get("opportunity_creation_decision") or "") or None
    return {
        "planning_outcome": outcome.outcome.value,
        "planning_outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
        "planning_decision_raw": str(decision_raw) if decision_raw is not None else None,
        "planning_status_raw": str(status_raw) if status_raw is not None else None,
        "planning_subtype": facts.get("planning_subtype"),
        "opportunity_creation_decision": decision,
        "evidence_support_classification": support.value,
        "planning_consistency_warning": (
            signal.get("relationship_status") == "ACTIVE"
            and decision == "CREATE_OPPORTUNITY"
            and outcome.outcome in NEGATIVE_PLANNING_OUTCOMES
        ),
    }
