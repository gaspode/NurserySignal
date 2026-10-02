from __future__ import annotations

from collections import Counter
from typing import Any

POLICY_VERSION = "care-opportunity-orphan-cleanup-v2"
AUTO_RESOLVE = "AUTO_RESOLVE"
PRESERVE = "PRESERVE"
ALREADY_RESOLVED = "ALREADY_RESOLVED"

SUPPORTED_ROOT_CAUSES = {
    "SIGNAL_REJECTED",
    "PLANNING_REFUSED",
    "PLANNING_WITHDRAWN",
    "TAXONOMY_RECLASSIFIED",
    "OLD_CREATION_RULE",
    "RELATIONSHIP_REMOVED",
    "NO_ACTIVE_SUPPORT",
}


def evaluate_orphan_cleanup(item: dict[str, Any]) -> dict[str, Any]:
    """Return a conservative, deterministic resolution decision for one audit item."""
    reasons: list[str] = []
    lifecycle = str(item.get("customer_lifecycle_stage") or "")
    if lifecycle not in {"STOPPED", "NEEDS_REVIEW"}:
        reasons.append("lifecycle_not_terminal_or_review")
    if item.get("review_status") == "REJECTED" and not reasons:
        return _decision(ALREADY_RESOLVED, "already_terminal", item)
    if item.get("category") != "UNSUPPORTED_ORPHAN_CANDIDATE":
        reasons.append("not_unsupported_orphan")
    if item.get("publication_status") != "DRAFT" or item.get("customer_published_at"):
        reasons.append("publication_history")
    if item.get("customer_withdrawn_at") or item.get("publication_automation_provenance"):
        reasons.append("publication_history")
    if item.get("customer_title") or item.get("customer_summary"):
        reasons.append("customer_content_present")
    if int(item.get("customer_saved_count") or 0):
        reasons.append("customer_saved_state")
    if item.get("publication_automation_blocked"):
        reasons.append("manual_automation_block")
    if item.get("admin_touch_types"):
        reasons.append("manual_or_admin_touch")
    if int(item.get("pending_match_reviews") or 0):
        reasons.append("pending_match_review")
    if int(item.get("enabled_planning_watches") or 0):
        reasons.append("active_planning_watch")
    if int(item.get("foundational_signal_count") or 0):
        reasons.append("valid_foundational_evidence")
    if item.get("root_cause") not in SUPPORTED_ROOT_CAUSES:
        reasons.append("unsupported_root_cause")

    root = item.get("root_cause")
    approved = int(item.get("active_approved_signal_count") or 0)
    supporting = int(item.get("supporting_followup_count") or 0)
    # An approved signal is only safe here when the hygiene classifier has
    # deterministically established that every active item is support-only.
    safe_terminal_outcome = (
        root in {"PLANNING_REFUSED", "PLANNING_WITHDRAWN"}
        and lifecycle == "STOPPED"
        and not int(item.get("enabled_planning_watches") or 0)
    )
    if (
        approved
        and root not in {"TAXONOMY_RECLASSIFIED", "OLD_CREATION_RULE"}
        and not safe_terminal_outcome
    ):
        reasons.append("approved_active_evidence")
    if supporting and root not in {"TAXONOMY_RECLASSIFIED", "OLD_CREATION_RULE"}:
        reasons.append("supporting_evidence")

    if reasons:
        return _decision(PRESERVE, reasons[0], item, reasons)
    return _decision(AUTO_RESOLVE, str(root or "unsupported").lower(), item)


def summarize_orphan_cleanup(items: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = [evaluate_orphan_cleanup(item) for item in items]
    return {
        "policy_version": POLICY_VERSION,
        "candidate_count": len(items),
        "outcome_counts": dict(sorted(Counter(d["outcome"] for d in decisions).items())),
        "root_cause_counts": dict(
            sorted(Counter(str(d["root_cause"] or "OTHER") for d in decisions).items())
        ),
        "preservation_reason_counts": dict(
            sorted(
                Counter(
                    reason
                    for decision in decisions
                    if decision["outcome"] == PRESERVE
                    for reason in decision["reasons"]
                ).items()
            )
        ),
        "decisions": decisions,
    }


def _decision(
    outcome: str,
    reason: str,
    item: dict[str, Any],
    reasons: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "opportunity_id": str(item.get("opportunity_id") or item.get("id")),
        "name": item.get("name"),
        "outcome": outcome,
        "reason": reason,
        "reasons": reasons or [reason],
        "root_cause": item.get("root_cause"),
        "publication_status": item.get("publication_status"),
        "customer_lifecycle_stage": item.get("customer_lifecycle_stage"),
        "active_relationships": int(item.get("active_relationships") or 0),
        "active_approved_signal_count": int(item.get("active_approved_signal_count") or 0),
        "foundational_signal_count": int(item.get("foundational_signal_count") or 0),
        "supporting_followup_count": int(item.get("supporting_followup_count") or 0),
    }
