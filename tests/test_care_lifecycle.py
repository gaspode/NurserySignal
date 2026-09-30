from uuid import UUID

from app.care_lifecycle import (
    CareLifecycle,
    derive_care_lifecycle,
    evaluate_publication,
    evaluate_withdrawal,
    publication_qa_holdout,
)


def planning(
    outcome: str,
    *,
    signal_id: str = "planning-1",
    review_status: str = "APPROVED",
    decision: str = "CREATE_OPPORTUNITY",
    subtype: str = "NEW_HOME_CHANGE_OF_USE",
) -> dict:
    return {
        "id": signal_id,
        "source_type": "planning",
        "relationship_status": "ACTIVE",
        "review_status": review_status,
        "metadata": {"decision": outcome},
        "extracted_facts": {
            "planning_subtype": subtype,
            "opportunity_creation_decision": decision,
        },
    }


def opportunity(**overrides) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "vertical": "CHILDRENS_HOME",
        "publication_status": "DRAFT",
        "change_type": "OPENING",
        "town": "Nottingham",
        "postcode": "NG8 1LD",
        "address": "10 Kingswood Road",
        "publication_automation_blocked": False,
        **overrides,
    }


def test_planning_lifecycle_transitions_are_deterministic() -> None:
    assert derive_care_lifecycle([planning("Pending")]).lifecycle == CareLifecycle.PLANNING_PENDING
    assert derive_care_lifecycle([planning("Granted")]).lifecycle == CareLifecycle.PLANNING_APPROVED
    assert derive_care_lifecycle([planning("Refused")]).lifecycle == CareLifecycle.STOPPED
    assert derive_care_lifecycle([planning("Withdrawn")]).lifecycle == CareLifecycle.STOPPED
    assert (
        derive_care_lifecycle(
            [
                planning("Refused", signal_id="refused"),
                planning("Appeal Lodged: REFUSE", signal_id="appeal"),
            ]
        ).lifecycle
        == CareLifecycle.APPEAL_PENDING
    )
    assert (
        derive_care_lifecycle([planning("Appeal Allowed")]).lifecycle
        == CareLifecycle.PLANNING_APPROVED
    )
    assert derive_care_lifecycle([planning("Appeal Dismissed")]).lifecycle == CareLifecycle.STOPPED


def test_stronger_evidence_prevents_planning_downgrade() -> None:
    recruitment = {
        "id": "job-1",
        "source_type": "recruitment",
        "relationship_status": "ACTIVE",
        "review_status": "APPROVED",
        "extracted_facts": {
            "recruitment_relevance": "RELEVANT_CHANGE",
            "commercial_change_evidence": "STRONG",
        },
    }
    routine = {
        **recruitment,
        "id": "job-2",
        "extracted_facts": {
            "recruitment_relevance": "RELEVANT_ROUTINE",
            "commercial_change_evidence": "NONE",
        },
    }
    registered = {
        "id": "ofsted-1",
        "source_type": "ofsted",
        "relationship_status": "ACTIVE",
        "review_status": "APPROVED",
        "metadata": {"registration_status": "Active"},
    }
    assert (
        derive_care_lifecycle([planning("Refused"), recruitment]).lifecycle
        == CareLifecycle.DELIVERY_SIGNAL_DETECTED
    )
    assert (
        derive_care_lifecycle([planning("Pending"), routine]).lifecycle
        == CareLifecycle.PLANNING_PENDING
    )
    assert (
        derive_care_lifecycle([planning("Refused"), registered]).lifecycle
        == CareLifecycle.REGISTERED
    )


def test_support_only_and_rejected_signals_do_not_create_lifecycle() -> None:
    support = planning("Approved", decision="SUPPORT_EXISTING_ONLY", subtype="CONDITION_DISCHARGE")
    rejected = planning("Approved", review_status="REJECTED")
    assert derive_care_lifecycle([support]).lifecycle == CareLifecycle.NEEDS_REVIEW
    assert derive_care_lifecycle([rejected]).lifecycle == CareLifecycle.NEEDS_REVIEW
    assert (
        derive_care_lifecycle([planning("Refused", review_status="REJECTED")]).lifecycle
        == CareLifecycle.STOPPED
    )
    rejected_capacity = planning(
        "Approved",
        review_status="REJECTED",
        subtype="EXPANSION_OR_CAPACITY_CHANGE",
    )
    assert derive_care_lifecycle([rejected_capacity]).lifecycle == CareLifecycle.NEEDS_REVIEW


def test_publication_policy_is_safe_stable_and_idempotent() -> None:
    lifecycle = derive_care_lifecycle([planning("Pending")])
    item = opportunity()
    first = evaluate_publication(
        item,
        lifecycle,
        [planning("Pending")],
        hygiene_category="VALID_SUPPORTED",
        hygiene_warning=None,
        safe_title="New children's home — Nottingham, NG8",
        safe_summary="A planning application has been submitted for a children's home.",
    )
    assert first.outcome in {"AUTO_PUBLISH", "QA_HOLDOUT"}
    assert publication_qa_holdout(item["id"]) == publication_qa_holdout(item["id"])
    assert (
        evaluate_publication(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="New children's home — Nottingham, NG8",
            safe_summary="A planning application has been submitted for a children's home.",
        )
        == first
    )

    assert (
        evaluate_publication(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="UNSUPPORTED_ORPHAN_CANDIDATE",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).outcome
        == "NOT_ELIGIBLE"
    )
    assert (
        "FULL_POSTCODE_LEAK"
        in evaluate_publication(
            item,
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="New home — NG8 1LD",
            safe_summary="Safe",
        ).exclusions
    )
    assert (
        "MANUAL_BLOCK"
        in evaluate_publication(
            opportunity(publication_automation_blocked=True),
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).exclusions
    )
    assert (
        "INITIAL_POLICY_SCOPE"
        in evaluate_publication(
            opportunity(change_type="EXPANSION"),
            lifecycle,
            [planning("Pending")],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).exclusions
    )
    existing = planning("Approved", subtype="LAWFULNESS_EXISTING", decision="SUPPORT_EXISTING_ONLY")
    assert (
        "INITIAL_POLICY_SCOPE"
        in evaluate_publication(
            item,
            derive_care_lifecycle([existing]),
            [existing],
            hygiene_category="VALID_SUPPORTED",
            hygiene_warning=None,
            safe_title="Safe",
            safe_summary="Safe",
        ).exclusions
    )


def test_withdrawal_requires_published_stopped_and_respects_alternatives() -> None:
    stopped = derive_care_lifecycle([planning("Refused")])
    assert (
        evaluate_withdrawal(opportunity(publication_status="PUBLISHED"), stopped).outcome
        == "AUTO_WITHDRAW"
    )
    blocked = opportunity(publication_status="PUBLISHED", publication_automation_blocked=True)
    assert evaluate_withdrawal(blocked, stopped).outcome == "MANUAL_REVIEW"
    manual = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="admin@example.com",
        publication_automation_provenance={},
    )
    assert evaluate_withdrawal(manual, stopped).outcome == "MANUAL_REVIEW"
    automated = opportunity(
        publication_status="PUBLISHED",
        customer_published_by="automation",
        publication_automation_provenance={"policy_version": "care-opportunity-publication-v1"},
    )
    assert evaluate_withdrawal(automated, stopped).outcome == "AUTO_WITHDRAW"
    approved = derive_care_lifecycle([planning("Approved")])
    assert (
        evaluate_withdrawal(opportunity(publication_status="PUBLISHED"), approved).outcome
        == "KEEP_PUBLISHED"
    )


def test_holdout_is_approximately_ten_percent() -> None:
    ids = [str(UUID(int=value)) for value in range(1, 1001)]
    held = sum(publication_qa_holdout(value) for value in ids)
    assert 70 <= held <= 130
