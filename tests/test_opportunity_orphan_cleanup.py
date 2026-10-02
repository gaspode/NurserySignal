from __future__ import annotations

from app.opportunity_orphan_cleanup import (
    ALREADY_RESOLVED,
    AUTO_RESOLVE,
    POLICY_VERSION,
    PRESERVE,
    evaluate_orphan_cleanup,
    summarize_orphan_cleanup,
)


def orphan(root: str, **overrides):
    item = {
        "opportunity_id": "00000000-0000-0000-0000-000000000001",
        "name": "Unsupported shell",
        "category": "UNSUPPORTED_ORPHAN_CANDIDATE",
        "root_cause": root,
        "review_status": "UNREVIEWED",
        "publication_status": "DRAFT",
        "customer_published_at": None,
        "customer_withdrawn_at": None,
        "customer_title": None,
        "customer_summary": None,
        "publication_automation_provenance": {},
        "publication_automation_blocked": False,
        "customer_lifecycle_stage": "NEEDS_REVIEW",
        "customer_saved_count": 0,
        "enabled_planning_watches": 0,
        "pending_match_reviews": 0,
        "admin_touch_types": [],
        "foundational_signal_count": 0,
        "supporting_followup_count": 0,
        "active_approved_signal_count": 0,
        "active_relationships": 1,
    }
    item.update(overrides)
    return item


def test_terminal_negative_or_rejected_orphans_are_auto_resolvable() -> None:
    for cause in ("SIGNAL_REJECTED", "PLANNING_REFUSED", "PLANNING_WITHDRAWN"):
        assert evaluate_orphan_cleanup(orphan(cause))["outcome"] == AUTO_RESOLVE


def test_taxonomy_and_old_rule_support_only_shells_are_auto_resolvable() -> None:
    for cause in ("TAXONOMY_RECLASSIFIED", "OLD_CREATION_RULE"):
        result = evaluate_orphan_cleanup(
            orphan(cause, active_approved_signal_count=1, supporting_followup_count=1)
        )
        assert result["outcome"] == AUTO_RESOLVE


def test_manual_published_customer_and_valid_evidence_are_preserved() -> None:
    cases = (
        orphan("SIGNAL_REJECTED", admin_touch_types=["manual_link"]),
        orphan("SIGNAL_REJECTED", publication_status="PUBLISHED"),
        orphan("SIGNAL_REJECTED", customer_saved_count=1),
        orphan("SIGNAL_REJECTED", foundational_signal_count=1),
        orphan("SIGNAL_REJECTED", active_approved_signal_count=1),
        orphan("SIGNAL_REJECTED", publication_automation_blocked=True),
        orphan("SIGNAL_REJECTED", enabled_planning_watches=1),
        orphan("SIGNAL_REJECTED", customer_lifecycle_stage="APPEAL_PENDING"),
    )
    assert all(evaluate_orphan_cleanup(item)["outcome"] == PRESERVE for item in cases)


def test_repeat_resolution_is_idempotent() -> None:
    result = evaluate_orphan_cleanup(orphan("SIGNAL_REJECTED", review_status="REJECTED"))
    assert result["outcome"] == ALREADY_RESOLVED
    assert result["reason"] == "already_terminal"


def test_summary_is_versioned_and_explainable() -> None:
    report = summarize_orphan_cleanup(
        [
            orphan("SIGNAL_REJECTED"),
            orphan("PLANNING_REFUSED", customer_saved_count=1),
        ]
    )
    assert report["policy_version"] == POLICY_VERSION
    assert report["outcome_counts"] == {"AUTO_RESOLVE": 1, "PRESERVE": 1}
    assert report["preservation_reason_counts"] == {"customer_saved_state": 1}
