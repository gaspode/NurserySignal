from __future__ import annotations

import pytest
from app.evidence_support import (
    EvidenceSupport,
    classify_evidence_support,
    planning_timeline_projection,
)
from app.opportunity_hygiene import audit_opportunities


def planning_signal(
    *,
    subtype: str = "NEW_HOME_CHANGE_OF_USE",
    decision: str = "CREATE_OPPORTUNITY",
    review_status: str = "APPROVED",
    planning_status: str = "Approved",
    family_relationships: list[str] | None = None,
) -> dict:
    return {
        "signal_id": "signal-1",
        "status": "ACTIVE",
        "relationship_status": "ACTIVE",
        "review_status": review_status,
        "source_type": "planning",
        "metadata": {"planning_status": planning_status},
        "extracted_facts": {
            "planning_subtype": subtype,
            "opportunity_creation_decision": decision,
        },
        "planning_family_relationship_types": family_relationships or [],
    }


@pytest.mark.parametrize(
    "subtype",
    [
        "NEW_HOME_CHANGE_OF_USE",
        "NEW_HOME_OTHER_EXPLICIT",
        "NEW_HOME_MIXED_USE",
        "LAWFULNESS_PROPOSED",
        "EXPANSION_OR_CAPACITY_CHANGE",
    ],
)
def test_approved_create_opportunity_planning_is_foundational(subtype: str) -> None:
    assert classify_evidence_support(planning_signal(subtype=subtype)) is (
        EvidenceSupport.FOUNDATIONAL
    )


@pytest.mark.parametrize(
    "description",
    [
        "C3 to C2 children's home",
        "HMO C4 to C2 residential home for children",
        "PRU educational facility to C2 teenage children's home",
        "Proposed change to a residential home for children",
    ],
)
def test_non_c3_wording_does_not_gate_stored_foundational_semantics(description: str) -> None:
    signal = planning_signal()
    signal["title"] = description
    assert classify_evidence_support(signal) is EvidenceSupport.FOUNDATIONAL


def test_rejected_capacity_signal_is_not_supporting_despite_ai_approve() -> None:
    signal = planning_signal(
        subtype="EXPANSION_OR_CAPACITY_CHANGE",
        review_status="REJECTED",
    )
    signal.update({"ai_recommendation": "APPROVE", "ai_confidence": 0.95})
    assert classify_evidence_support(signal) is EvidenceSupport.NON_SUPPORTING


def test_approved_review_uses_immutable_relationship_create_semantics() -> None:
    signal = planning_signal(subtype="AMBIGUOUS", decision="REVIEW")
    signal["relationship_extracted_facts"] = {
        "opportunity_creation_decision": "CREATE_OPPORTUNITY"
    }
    assert classify_evidence_support(signal) is EvidenceSupport.FOUNDATIONAL


def test_rejected_review_does_not_use_relationship_create_semantics() -> None:
    signal = planning_signal(
        subtype="AMBIGUOUS", decision="REVIEW", review_status="REJECTED"
    )
    signal["relationship_extracted_facts"] = {
        "opportunity_creation_decision": "CREATE_OPPORTUNITY"
    }
    assert classify_evidence_support(signal) is EvidenceSupport.NON_SUPPORTING


def test_current_support_only_overrides_old_relationship_create_semantics() -> None:
    signal = planning_signal(
        subtype="CONDITION_DISCHARGE", decision="SUPPORT_EXISTING_ONLY"
    )
    signal["relationship_extracted_facts"] = {
        "opportunity_creation_decision": "CREATE_OPPORTUNITY"
    }
    assert classify_evidence_support(signal) is EvidenceSupport.SUPPORTING_FOLLOWUP


@pytest.mark.parametrize(
    "subtype",
    [
        "CONDITION_DISCHARGE",
        "CONDITION_VARIATION",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
        "LAWFULNESS_EXISTING",
    ],
)
def test_approved_support_existing_planning_is_supporting(subtype: str) -> None:
    signal = planning_signal(subtype=subtype, decision="SUPPORT_EXISTING_ONLY")
    assert classify_evidence_support(signal) is EvidenceSupport.SUPPORTING_FOLLOWUP


@pytest.mark.parametrize(
    "planning_status",
    ["Refused", "Withdrawn", "Appeal dismissed", "Appeal lodged: REFUSE"],
)
def test_negative_planning_outcomes_are_other_lifecycle(planning_status: str) -> None:
    signal = planning_signal(planning_status=planning_status)
    assert classify_evidence_support(signal) is EvidenceSupport.OTHER_LIFECYCLE


def test_cessation_is_not_positive_support() -> None:
    signal = planning_signal(subtype="CESSATION_OR_CHANGE_AWAY_FROM_CARE")
    assert classify_evidence_support(signal) is EvidenceSupport.OTHER_LIFECYCLE


def test_existing_lawfulness_cannot_become_foundation_from_create_decision_alone() -> None:
    signal = planning_signal(subtype="LAWFULNESS_EXISTING")
    assert classify_evidence_support(signal) is EvidenceSupport.NON_SUPPORTING


def test_direct_opening_needs_no_planning_family() -> None:
    assert classify_evidence_support(planning_signal(family_relationships=[])) is (
        EvidenceSupport.FOUNDATIONAL
    )


def test_primary_family_signal_is_counted_once() -> None:
    signal = planning_signal(family_relationships=["PRIMARY_APPLICATION"])
    states = [classify_evidence_support(signal)]
    assert states.count(EvidenceSupport.FOUNDATIONAL) == 1


def test_referenced_application_is_supporting_not_foundational() -> None:
    signal = planning_signal(family_relationships=["REFERENCES_APPLICATION"])
    assert classify_evidence_support(signal) is EvidenceSupport.SUPPORTING_FOLLOWUP


def test_hygiene_uses_same_foundational_semantics() -> None:
    relationship = planning_signal(subtype="LAWFULNESS_PROPOSED")
    opportunity = {
        "id": "opportunity-1",
        "name": "New children's home",
        "review_status": "UNREVIEWED",
        "publication_status": "DRAFT",
        "lifecycle_stage": "PLANNING",
        "change_type": "OPENING",
        "operator_name": None,
        "postcode": "AA1 1AA",
        "town": "Exampletown",
        "relationships": [relationship],
        "history_actions": [],
        "audit_actions": [],
    }

    report = audit_opportunities([opportunity])

    assert classify_evidence_support(relationship) is EvidenceSupport.FOUNDATIONAL
    assert report["items"][0]["category"] == "VALID_SUPPORTED"
    assert report["items"][0]["supporting_signal_count"] == 1


def test_planning_timeline_projects_canonical_and_raw_semantics() -> None:
    signal = planning_signal(subtype="NEW_HOME_CHANGE_OF_USE")
    signal["metadata"] = {
        "decision": "Grant Permission Subject To Conditions",
        "planning_status": "Decided",
    }

    projection = planning_timeline_projection(signal)

    assert projection == {
        "planning_outcome": "APPROVED",
        "planning_outcome_policy_version": "planning-outcome-v1",
        "planning_decision_raw": "Grant Permission Subject To Conditions",
        "planning_status_raw": "Decided",
        "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
        "evidence_support_classification": "FOUNDATIONAL",
        "planning_consistency_warning": False,
    }


def test_planning_timeline_flags_negative_create_semantics() -> None:
    signal = planning_signal(planning_status="Refused")
    projection = planning_timeline_projection(signal)
    assert projection["planning_outcome"] == "REFUSED"
    assert projection["planning_status_raw"] == "Refused"
    assert projection["planning_consistency_warning"] is True


def test_planning_timeline_missing_fields_and_non_planning_degrade_cleanly() -> None:
    signal = planning_signal()
    signal["metadata"] = {}
    signal["extracted_facts"] = {}
    projection = planning_timeline_projection(signal)
    assert projection["planning_outcome"] == "UNKNOWN"
    assert projection["planning_decision_raw"] is None
    assert projection["planning_status_raw"] is None
    assert projection["planning_subtype"] is None
    assert projection["opportunity_creation_decision"] is None
    assert planning_timeline_projection({"source_type": "recruitment"}) == {}
