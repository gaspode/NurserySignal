from __future__ import annotations

from uuid import uuid4

from app.opportunity_semantic_drift import plan_opportunity_semantic_drift


def planning_relation(
    *,
    subtype: str,
    decision: str,
    review_status: str = "APPROVED",
    recomputed_subtype: str | None = None,
    historical_decision: str = "CREATE_OPPORTUNITY",
) -> dict:
    return {
        "signal_id": str(uuid4()),
        "status": "ACTIVE",
        "source_type": "planning",
        "review_status": review_status,
        "metadata": {"decision": "Approved"},
        "extracted_facts": {
            "planning_subtype": subtype,
            "opportunity_creation_decision": decision,
        },
        "relationship_extracted_facts": {
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
            "opportunity_creation_decision": historical_decision,
        },
        "planning_family_relationship_types": [],
        "recomputed_planning_subtype": recomputed_subtype or subtype,
    }


def opportunity(*relations: dict, **overrides) -> dict:
    value = {
        "id": str(uuid4()),
        "vertical": "CHILDRENS_HOME",
        "name": "New children's home — Example Road",
        "event_type": "opening",
        "lifecycle_stage": "PLANNING",
        "change_type": "OPENING",
        "publication_status": "DRAFT",
        "stage_reason": "Planning evidence indicates a new children's home",
        "creation_reason": "Created from original planning evidence",
        "admin_touch_types": [],
        "relationships": list(relations),
    }
    value.update(overrides)
    return value


def test_existing_lawfulness_only_is_corrected_to_non_opening() -> None:
    plan = plan_opportunity_semantic_drift(
        opportunity(
            planning_relation(
                subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY"
            )
        )
    )
    assert plan is not None
    assert plan["safe_to_apply"] is True
    assert plan["proposed"]["change_type"] == "OTHER_CHANGE"
    assert plan["proposed"]["event_type"] == "other"
    assert plan["proposed"]["name"] == "Children's home — Example Road"
    assert "existing children's-home use" in plan["proposed"]["stage_reason"]


def test_existing_lawfulness_with_valid_opening_foundation_is_preserved() -> None:
    plan = plan_opportunity_semantic_drift(
        opportunity(
            planning_relation(
                subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY"
            ),
            planning_relation(
                subtype="NEW_HOME_CHANGE_OF_USE", decision="CREATE_OPPORTUNITY"
            ),
        )
    )
    assert plan is not None
    assert plan["action"] == "PRESERVE_OPENING_VALID_FOUNDATION"
    assert plan["safe_to_apply"] is False


def test_rejected_cessation_without_foundation_is_neutralised_not_closed() -> None:
    plan = plan_opportunity_semantic_drift(
        opportunity(
            planning_relation(
                subtype="CESSATION_OR_CHANGE_AWAY_FROM_CARE",
                decision="IGNORE_FOR_OPPORTUNITY",
                review_status="REJECTED",
            )
        )
    )
    assert plan is not None
    assert plan["safe_to_apply"] is True
    assert plan["proposed"]["change_type"] == "OTHER_CHANGE"
    assert plan["proposed"]["lifecycle_stage"] == "PLANNING"
    assert "No current approved foundational evidence" in plan["proposed"]["stage_reason"]


def test_rejected_cessation_with_prior_valid_foundation_preserves_opening() -> None:
    plan = plan_opportunity_semantic_drift(
        opportunity(
            planning_relation(
                subtype="CESSATION_OR_CHANGE_AWAY_FROM_CARE",
                decision="IGNORE_FOR_OPPORTUNITY",
                review_status="REJECTED",
            ),
            planning_relation(
                subtype="NEW_HOME_OTHER_EXPLICIT", decision="CREATE_OPPORTUNITY"
            ),
        )
    )
    assert plan is not None
    assert plan["action"] == "PRESERVE_OPENING_VALID_FOUNDATION"


def test_rejected_capacity_human_decision_wins_over_historical_creation() -> None:
    relation = planning_relation(
        subtype="AMBIGUOUS",
        decision="REVIEW",
        review_status="REJECTED",
        recomputed_subtype="EXPANSION_OR_CAPACITY_CHANGE",
    )
    relation["ai_recommendation"] = "APPROVE"
    plan = plan_opportunity_semantic_drift(opportunity(relation))
    assert plan is not None
    assert plan["drift_types"] == ["REJECTED_CAPACITY_ON_OPENING"]
    assert plan["safe_to_apply"] is True
    assert plan["proposed"]["change_type"] == "OTHER_CHANGE"


def test_published_or_admin_touched_candidates_require_manual_review() -> None:
    relation = planning_relation(
        subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY"
    )
    published = plan_opportunity_semantic_drift(
        opportunity(relation, publication_status="PUBLISHED")
    )
    touched = plan_opportunity_semantic_drift(
        opportunity(relation, admin_touch_types=["manual_link"])
    )
    assert published and published["safety_blocker"] == "PUBLISHED"
    assert touched and touched["safety_blocker"] == "ADMIN_TOUCHED"
    assert not published["safe_to_apply"] and not touched["safe_to_apply"]


def test_repeat_after_correction_is_idempotent() -> None:
    item = opportunity(
        planning_relation(
            subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY"
        )
    )
    plan = plan_opportunity_semantic_drift(item)
    assert plan is not None
    item.update(plan["proposed"])
    assert plan_opportunity_semantic_drift(item) is None
