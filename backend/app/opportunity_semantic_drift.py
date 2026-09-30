from __future__ import annotations

import re
from typing import Any

from app.evidence_support import EvidenceSupport, classify_evidence_support
from app.planning_outcomes import canonical_planning_outcome

POLICY_VERSION = "care-opportunity-semantic-drift-v1"


def neutral_care_opportunity_name(name: str) -> str | None:
    """Return the non-opening form of a system-generated CareProspect name."""
    value = str(name or "").strip()
    updated = re.sub(
        r"^New children(?:'|’)s home(?=\s*(?:—|-|$))",
        "Children's home",
        value,
        count=1,
        flags=re.IGNORECASE,
    )
    return updated if updated != value else None


def _relation_summary(relation: dict[str, Any]) -> dict[str, Any]:
    facts = relation.get("extracted_facts") or {}
    historical = relation.get("relationship_extracted_facts") or {}
    return {
        "signal_id": str(relation.get("signal_id") or ""),
        "relationship_status": relation.get("status"),
        "review_status": relation.get("review_status"),
        "planning_subtype": facts.get("planning_subtype"),
        "recomputed_planning_subtype": relation.get("recomputed_planning_subtype"),
        "opportunity_creation_decision": facts.get("opportunity_creation_decision"),
        "canonical_planning_outcome": canonical_planning_outcome(
            relation.get("metadata")
        ).outcome.value,
        "historical_relationship_planning_subtype": historical.get("planning_subtype"),
        "historical_relationship_creation_decision": historical.get(
            "opportunity_creation_decision"
        ),
        "support_state": classify_evidence_support(relation).value,
    }


def plan_opportunity_semantic_drift(
    opportunity: dict[str, Any],
) -> dict[str, Any] | None:
    """Plan one conservative correction without changing stored business state."""
    if str(opportunity.get("vertical") or "") != "CHILDRENS_HOME":
        return None
    if str(opportunity.get("change_type") or "") != "OPENING":
        return None

    active_planning = [
        relation
        for relation in opportunity.get("relationships") or []
        if relation.get("status") == "ACTIVE"
        and str(relation.get("source_type") or "").lower() == "planning"
    ]
    if not active_planning:
        return None

    existing_use = []
    cessation = []
    rejected_capacity = []
    for relation in active_planning:
        facts = relation.get("extracted_facts") or {}
        historical = relation.get("relationship_extracted_facts") or {}
        subtype = str(facts.get("planning_subtype") or "")
        recomputed = str(relation.get("recomputed_planning_subtype") or "")
        if subtype == "LAWFULNESS_EXISTING":
            existing_use.append(relation)
        if subtype == "CESSATION_OR_CHANGE_AWAY_FROM_CARE":
            cessation.append(relation)
        if (
            relation.get("review_status") == "REJECTED"
            and recomputed == "EXPANSION_OR_CAPACITY_CHANGE"
            and historical.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY"
            and facts.get("opportunity_creation_decision") != "CREATE_OPPORTUNITY"
        ):
            rejected_capacity.append(relation)

    drift_groups = {
        "LAWFULNESS_EXISTING_ON_OPENING": existing_use,
        "REJECTED_CESSATION_ON_OPENING": cessation,
        "REJECTED_CAPACITY_ON_OPENING": rejected_capacity,
    }
    present = [reason for reason, relations in drift_groups.items() if relations]
    if not present:
        return None

    foundational = [
        relation
        for relation in opportunity.get("relationships") or []
        if classify_evidence_support(relation) is EvidenceSupport.FOUNDATIONAL
    ]
    touches = list(opportunity.get("admin_touch_types") or [])
    published = opportunity.get("publication_status") == "PUBLISHED"
    generated_name = neutral_care_opportunity_name(str(opportunity.get("name") or ""))

    if foundational:
        action = "PRESERVE_OPENING_VALID_FOUNDATION"
        safe = False
        confidence = "HIGH"
        blocker = "SEPARATE_VALID_FOUNDATIONAL_EVIDENCE"
        proposed = None
    else:
        action = "CORRECT_TO_OTHER_CHANGE"
        safe = not published and not touches and generated_name is not None
        confidence = "HIGH" if len(present) == 1 else "MEDIUM"
        blocker = (
            "PUBLISHED"
            if published
            else "ADMIN_TOUCHED"
            if touches
            else "NON_STANDARD_TITLE"
            if generated_name is None
            else "MULTIPLE_DRIFT_CLASSES"
            if len(present) != 1
            else None
        )
        if len(present) != 1:
            safe = False
        approved_existing = any(
            relation.get("review_status") == "APPROVED" for relation in existing_use
        )
        stage_reason = (
            "Planning evidence confirms existing children's-home use; this is not a "
            "new-opening event."
            if present == ["LAWFULNESS_EXISTING_ON_OPENING"] and approved_existing
            else "No current approved foundational evidence supports a new-opening event."
        )
        proposed = {
            "name": generated_name,
            "change_type": "OTHER_CHANGE",
            "event_type": "other",
            # There is no unsupported/inactive lifecycle enum. Preserve the stage rather
            # than inventing closure or rewriting historical lifecycle evidence.
            "lifecycle_stage": opportunity.get("lifecycle_stage"),
            "stage_reason": stage_reason,
        }

    relevant_ids = {
        str(relation.get("signal_id"))
        for reason in present
        for relation in drift_groups[reason]
    }
    return {
        "policy_version": POLICY_VERSION,
        "opportunity_id": str(opportunity.get("id")),
        "current": {
            "name": opportunity.get("name"),
            "change_type": opportunity.get("change_type"),
            "event_type": opportunity.get("event_type"),
            "lifecycle_stage": opportunity.get("lifecycle_stage"),
            "stage_reason": opportunity.get("stage_reason"),
            "creation_reason": opportunity.get("creation_reason"),
            "publication_status": opportunity.get("publication_status"),
        },
        "drift_types": present,
        "action": action,
        "confidence": confidence,
        "safe_to_apply": safe,
        "safety_blocker": blocker,
        "admin_touch_types": touches,
        "active_signal_ids": [
            str(relation.get("signal_id"))
            for relation in opportunity.get("relationships") or []
            if relation.get("status") == "ACTIVE"
        ],
        "foundational_signal_ids": [str(item.get("signal_id")) for item in foundational],
        "drift_signal_ids": sorted(relevant_ids),
        "signals": [_relation_summary(item) for item in active_planning],
        "proposed": proposed,
    }
