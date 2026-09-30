from __future__ import annotations

from uuid import uuid4

from app.opportunity_hygiene import audit_opportunities


def relation(
    *,
    source_type: str = "planning",
    status: str = "ACTIVE",
    review_status: str = "APPROVED",
    subtype: str = "NEW_HOME_CHANGE_OF_USE",
    decision: str = "CREATE_OPPORTUNITY",
    planning_status: str = "Approved",
) -> dict:
    return {
        "signal_id": str(uuid4()),
        "status": status,
        "review_status": review_status,
        "source_type": source_type,
        "metadata": {"planning_status": planning_status},
        "extracted_facts": {
            "planning_subtype": subtype,
            "opportunity_creation_decision": decision,
            "planning_taxonomy_version": "care-planning-taxonomy-v2",
        },
        "created_by": "SYSTEM",
    }


def opportunity(**overrides) -> dict:
    values = {
        "id": str(uuid4()),
        "nursery_id": None,
        "name": "New children's home",
        "review_status": "UNREVIEWED",
        "publication_status": "DRAFT",
        "lifecycle_stage": "PLANNING",
        "change_type": "OPENING",
        "operator_name": "Example Care Ltd",
        "postcode": "AA1 1AA",
        "town": "Exampletown",
        "creation_reason": "Created from approved planning evidence",
        "relationships": [relation()],
        "history_actions": [],
        "audit_actions": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    values.update(overrides)
    return values


def test_hygiene_categories_are_mutually_exclusive_and_conservative() -> None:
    valid = opportunity(operator_name="Valid Care Ltd", postcode="V1 1AA")
    refused = opportunity(
        operator_name="Refused Care Ltd",
        postcode="R1 1AA",
        relationships=[relation(planning_status="Planning Permission - Refused")],
    )
    followup = opportunity(
        operator_name="Followup Care Ltd",
        postcode="F1 1AA",
        relationships=[
            relation(subtype="CONDITION_DISCHARGE", decision="SUPPORT_EXISTING_ONLY")
        ],
    )
    pending = opportunity(
        operator_name="Pending Care Ltd",
        postcode="P1 1AA",
        relationships=[relation(review_status="PENDING")],
    )
    published = opportunity(publication_status="PUBLISHED", customer_published_at="2026-01-02")
    report = audit_opportunities([valid, refused, followup, pending, published])
    by_id = {item["opportunity_id"]: item for item in report["items"]}
    assert by_id[str(valid["id"])]["category"] == "VALID_SUPPORTED"
    assert by_id[str(refused["id"])]["root_cause"] == "PLANNING_REFUSED"
    assert by_id[str(followup["id"])]["root_cause"] == "TAXONOMY_RECLASSIFIED"
    assert by_id[str(pending["id"])]["category"] == "NEEDS_INVESTIGATION"
    assert by_id[str(published["id"])]["category"] == "MANUAL_OR_ADMIN_TOUCHED_PRESERVE"
    assert sum(report["category_counts"].values()) == 5


def test_exact_identity_duplicate_keeps_stronger_canonical_opportunity() -> None:
    canonical = opportunity(publication_status="PUBLISHED", customer_published_at="2026-01-02")
    duplicate = opportunity(created_at="2026-02-01T00:00:00Z")
    report = audit_opportunities([canonical, duplicate])
    duplicate_item = next(
        item for item in report["items"] if item["opportunity_id"] == str(duplicate["id"])
    )
    assert duplicate_item["category"] == "DUPLICATE_CANDIDATE"
    assert duplicate_item["duplicate"]["canonical_opportunity_id"] == str(canonical["id"])


def test_historical_age_does_not_make_supported_opportunity_an_orphan() -> None:
    historical = opportunity(created_at="2020-01-01T00:00:00Z", lifecycle_stage="OPEN")
    report = audit_opportunities([historical])
    assert report["items"][0]["category"] == "VALID_SUPPORTED"


def test_merged_opportunity_is_preserved_as_admin_touched() -> None:
    merged = opportunity(
        review_status="MERGED",
        merged_into_opportunity_id=str(uuid4()),
        relationships=[],
    )
    report = audit_opportunities([merged])
    assert report["items"][0]["category"] == "MANUAL_OR_ADMIN_TOUCHED_PRESERVE"
    assert report["items"][0]["admin_touch_types"] == ["merge"]
