"""Narrow, preview-first customer publication policy for NurserySignal."""

from __future__ import annotations

from collections import Counter
from typing import Any

POLICY_VERSION = "nursery-customer-publication-v1"
ELIGIBLE = "ELIGIBLE"


def nursery_customer_stage(row: dict[str, Any]) -> str:
    """Project a compact customer stage without changing internal lifecycle."""
    outcome = str(row.get("planning_outcome") or "").upper()
    source_types = set(row.get("source_types") or [])
    if outcome in {"APPROVED", "APPEAL_ALLOWED"} or str(row.get("lifecycle_stage")) == "APPROVED":
        return "PLANNING_APPROVED"
    if "recruitment" in source_types and str(row.get("lifecycle_stage")) in {
        "RECRUITING",
        "STAFFING",
        "FIT_OUT",
        "OPENING_SOON",
    }:
        return "RECRUITMENT_ACTIVITY"
    if outcome in {"PENDING", "UNKNOWN", "REFUSED_UNDER_APPEAL"} or str(
        row.get("lifecycle_stage")
    ) in {"PLANNING", "DISCOVERED"}:
        return "PLANNING_PENDING"
    return "OTHER_CHANGE"


def evaluate_nursery_customer_publication(row: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable, conservative result for one Nursery opportunity.

    This deliberately admits only reviewed Planning-origin material changes.  It
    does not try to make a customer product out of manual-review ambiguity.
    """
    facts = row.get("facts") or {}
    source_types = set(row.get("source_types") or [])
    outcome = str(row.get("planning_outcome") or "").upper()
    change_type = str(row.get("change_type") or "")
    text = " ".join(str(row.get(key) or "") for key in ("name", "signal_title")).casefold()
    reasons: list[str] = []
    if str(row.get("vertical")) != "NURSERY":
        reasons.append("wrong_vertical")
    if str(row.get("publication_status")) != "DRAFT":
        reasons.append("not_draft")
    if str(row.get("review_status")) in {"REJECTED", "MERGED"} or row.get(
        "merged_into_opportunity_id"
    ):
        reasons.append("inactive_or_merged")
    if not row.get("site_identity"):
        reasons.append("missing_site_identity")
    if int(row.get("approved_planning_count") or 0) < 1:
        reasons.append("no_approved_planning_evidence")
    if "planning" not in source_types:
        reasons.append("not_planning_origin")
    if change_type not in {"OPENING", "EXPANSION", "RELOCATION"}:
        reasons.append("change_type_outside_initial_scope")
    if outcome in {"REFUSED", "WITHDRAWN", "APPEAL_DISMISSED", "CESSATION"}:
        reasons.append("terminal_planning_outcome")
    if facts.get("likely_false_positive") or facts.get("planning_candidate_matched") is False:
        reasons.append("classification_excluded")
    # Text guard applies only to the conservative first cohort. It prevents
    # publication of an otherwise old shell whose remaining evidence does not
    # clearly identify childcare nursery use.
    nursery_terms = ("nursery", "pre-school", "preschool", "childcare", "early years")
    if not any(term in text for term in nursery_terms):
        reasons.append("no_explicit_nursery_semantics")
    ambiguity_tokens = (
        "mixed use",
        "mixed-use",
        "school",
        "horticultural",
        "lawful development",
        "certificate",
    )
    if any(token in text for token in ambiguity_tokens):
        reasons.append("initial_cohort_ambiguity_excluded")
    stage = nursery_customer_stage(row)
    return {
        "outcome": ELIGIBLE if not reasons else "EXCLUDED",
        "reasons": reasons or ["reviewed_explicit_nursery_planning_change"],
        "stage": stage,
        "policy_version": POLICY_VERSION,
    }


def preview_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(item["policy"]["outcome"] for item in items)
    reasons = Counter(reason for item in items for reason in item["policy"]["reasons"])
    stages = Counter(
        item["policy"]["stage"] for item in items if item["policy"]["outcome"] == ELIGIBLE
    )
    return {
        "outcomes": dict(outcomes),
        "exclusions_by_reason": dict(reasons),
        "eligible_by_stage": dict(stages),
    }
