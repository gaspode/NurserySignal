from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from app.planning_outcomes import PlanningOutcome, canonical_planning_outcome

HYGIENE_CATEGORIES = (
    "VALID_SUPPORTED",
    "DUPLICATE_CANDIDATE",
    "SUPERSEDED_CANDIDATE",
    "UNSUPPORTED_ORPHAN_CANDIDATE",
    "MANUAL_OR_ADMIN_TOUCHED_PRESERVE",
    "NEEDS_INVESTIGATION",
)

SUPPORT_ONLY_PLANNING_SUBTYPES = {
    "LAWFULNESS_EXISTING",
    "CONDITION_VARIATION",
    "CONDITION_DISCHARGE",
    "NON_MATERIAL_AMENDMENT",
    "FOLLOW_UP_OTHER",
    "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
}
NEGATIVE_OUTCOMES = {
    PlanningOutcome.REFUSED,
    PlanningOutcome.WITHDRAWN,
    PlanningOutcome.APPEAL_DISMISSED,
}


def _normalise(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _touch_types(opportunity: dict[str, Any]) -> list[str]:
    touches: set[str] = set()
    if opportunity.get("publication_status") == "PUBLISHED" or opportunity.get(
        "customer_published_at"
    ):
        touches.add("manual_publication")
    reason = str(opportunity.get("creation_reason") or "").lower()
    if "created manually" in reason or "admin" in reason:
        touches.add("manual_creation")
    for action in opportunity.get("history_actions") or []:
        action = str(action).upper()
        if "MERGE" in action:
            touches.add("merge")
        elif "SPLIT" in action:
            touches.add("split")
        elif "UNLINK" in action:
            touches.add("manual_unlink")
        elif "LINK" in action:
            touches.add("manual_link")
    for action in opportunity.get("audit_actions") or []:
        action = str(action).lower()
        if "merged" in action:
            touches.add("merge")
        elif "split" in action:
            touches.add("split")
        elif "unlinked" in action:
            touches.add("manual_unlink")
        elif "linked" in action:
            touches.add("manual_link")
        elif "created_from_signal" in action:
            touches.add("manual_creation")
        elif "publication" in action:
            touches.add("manual_publication")
        else:
            touches.add("other_correction")
    if any(
        relation.get("created_by") == "ADMIN" or relation.get("admin_override_by")
        for relation in opportunity.get("relationships") or []
    ):
        touches.add("other_correction")
    return sorted(touches)


def _relation_state(relation: dict[str, Any]) -> str:
    if relation.get("status") != "ACTIVE":
        return "INACTIVE"
    if relation.get("review_status") != "APPROVED":
        return "PENDING" if relation.get("review_status") == "PENDING" else "REJECTED"
    source_type = str(relation.get("source_type") or "").lower()
    if source_type == "procurement":
        return "INTERNAL_ONLY"
    if source_type == "planning":
        outcome = canonical_planning_outcome(relation.get("metadata")).outcome
        if outcome in NEGATIVE_OUTCOMES:
            return outcome.value
        if outcome is PlanningOutcome.REFUSED_UNDER_APPEAL:
            return "SUPPORT_ONLY"
        facts = relation.get("extracted_facts") or {}
        subtype = str(facts.get("planning_subtype") or "")
        decision = str(facts.get("opportunity_creation_decision") or "")
        if subtype in SUPPORT_ONLY_PLANNING_SUBTYPES or decision == "SUPPORT_EXISTING_ONLY":
            return "SUPPORT_ONLY"
        if decision == "IGNORE_FOR_OPPORTUNITY":
            return "INACTIVE"
        return "FOUNDATIONAL" if decision == "CREATE_OPPORTUNITY" else "INVESTIGATE"
    if source_type in {"recruitment", "ofsted"}:
        return "FOUNDATIONAL"
    return "SUPPORT_ONLY"


def _duplicate_counterparts(opportunities: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for opportunity in opportunities:
        if opportunity.get("review_status") in {"MERGED", "REJECTED"}:
            continue
        postcode = _normalise(opportunity.get("postcode"))
        operator = _normalise(opportunity.get("operator_name"))
        nursery_id = str(opportunity.get("nursery_id") or "")
        change_type = str(opportunity.get("change_type") or "")
        if nursery_id:
            groups[("SITE", nursery_id, change_type)].append(opportunity)
        elif postcode and operator:
            groups[("IDENTITY", postcode, operator, change_type)].append(opportunity)
    shared_signals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for opportunity in opportunities:
        for relation in opportunity.get("relationships") or []:
            if relation.get("status") == "ACTIVE":
                shared_signals[str(relation.get("signal_id"))].append(opportunity)
    for signal_id, members in shared_signals.items():
        if len(members) > 1:
            groups[("SIGNAL", signal_id)].extend(members)

    counterparts: dict[str, dict[str, Any]] = {}
    for key, raw_members in groups.items():
        members = list({str(item["id"]): item for item in raw_members}.values())
        if len(members) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda item: (
                item.get("publication_status") == "PUBLISHED",
                bool(_touch_types(item)),
                sum(_relation_state(rel) == "FOUNDATIONAL" for rel in item["relationships"]),
                str(item.get("created_at") or ""),
            ),
            reverse=True,
        )
        canonical = ranked[0]
        for candidate in ranked[1:]:
            candidate_id = str(candidate["id"])
            if candidate_id not in counterparts:
                counterparts[candidate_id] = {
                    "canonical_opportunity_id": str(canonical["id"]),
                    "reason": "shared active signal" if key[0] == "SIGNAL" else (
                        "same canonical site" if key[0] == "SITE" else
                        "same exact operator, postcode and change type"
                    ),
                    "shared_signal_ids": [key[1]] if key[0] == "SIGNAL" else [],
                }
    return counterparts


def audit_opportunities(opportunities: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify a complete vertical inventory without changing any stored state."""
    duplicates = _duplicate_counterparts(opportunities)
    items: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    root_causes: Counter[str] = Counter()
    touch_counts: Counter[str] = Counter()
    source_mix_counts: Counter[str] = Counter()
    publication_counts: Counter[str] = Counter()
    change_type_counts: Counter[str] = Counter()

    for opportunity in opportunities:
        opportunity_id = str(opportunity["id"])
        relationships = opportunity.get("relationships") or []
        states = [_relation_state(relation) for relation in relationships]
        foundational = sum(state == "FOUNDATIONAL" for state in states)
        active_approved = sum(
            relation.get("status") == "ACTIVE" and relation.get("review_status") == "APPROVED"
            for relation in relationships
        )
        sources = sorted(
            {
                str(relation.get("source_type") or "UNKNOWN").upper()
                for relation in relationships
                if relation.get("status") == "ACTIVE"
            }
        )
        source_mix = "+".join(sources) if sources else "NONE"
        touches = _touch_types(opportunity)
        for touch in touches:
            touch_counts[touch] += 1

        duplicate = duplicates.get(opportunity_id)
        root_cause = None
        warning = None
        if touches:
            category = "MANUAL_OR_ADMIN_TOUCHED_PRESERVE"
        elif opportunity.get("review_status") == "MERGED" or opportunity.get(
            "merged_into_opportunity_id"
        ):
            category = "SUPERSEDED_CANDIDATE"
        elif duplicate:
            category = "DUPLICATE_CANDIDATE"
        elif foundational:
            category = "VALID_SUPPORTED"
        elif any(state in {"PENDING", "INVESTIGATE"} for state in states):
            category = "NEEDS_INVESTIGATION"
            warning = "Active evidence is pending review or lacks decisive event semantics."
        else:
            category = "UNSUPPORTED_ORPHAN_CANDIDATE"
            if "REFUSED" in states or "APPEAL_DISMISSED" in states:
                root_cause = "PLANNING_REFUSED"
            elif "WITHDRAWN" in states:
                root_cause = "PLANNING_WITHDRAWN"
            elif relationships and all(state == "INACTIVE" for state in states):
                root_cause = "RELATIONSHIP_REMOVED"
            elif relationships and all(state == "REJECTED" for state in states):
                root_cause = "SIGNAL_REJECTED"
            elif states and all(state in {"SUPPORT_ONLY", "INTERNAL_ONLY"} for state in states):
                root_cause = (
                    "TAXONOMY_RECLASSIFIED"
                    if any(
                        (relation.get("extracted_facts") or {}).get(
                            "planning_taxonomy_version"
                        )
                        == "care-planning-taxonomy-v2"
                        for relation in relationships
                    )
                    else "OLD_CREATION_RULE"
                )
            elif not relationships:
                root_cause = "NO_ACTIVE_SUPPORT"
            else:
                root_cause = "OTHER"
            root_causes[root_cause] += 1

        if opportunity.get("publication_status") == "PUBLISHED" and foundational == 0:
            warning = "Published opportunity has no current foundational support."

        category_counts[category] += 1
        publication_counts[str(opportunity.get("publication_status") or "UNKNOWN")] += 1
        change_type_counts[str(opportunity.get("change_type") or "UNKNOWN")] += 1
        source_mix_counts[source_mix] += 1
        items.append(
            {
                "opportunity_id": opportunity_id,
                "name": opportunity.get("name"),
                "review_status": opportunity.get("review_status"),
                "publication_status": opportunity.get("publication_status"),
                "lifecycle_stage": opportunity.get("lifecycle_stage"),
                "change_type": opportunity.get("change_type"),
                "operator_name": opportunity.get("operator_name"),
                "postcode": opportunity.get("postcode"),
                "town": opportunity.get("town"),
                "category": category,
                "root_cause": root_cause,
                "supporting_signal_count": foundational,
                "active_approved_signal_count": active_approved,
                "source_types": sources,
                "source_mix": source_mix,
                "active_relationships": sum(
                    relation.get("status") == "ACTIVE" for relation in relationships
                ),
                "rejected_relationships": sum(
                    relation.get("status") == "REJECTED" for relation in relationships
                ),
                "admin_touch_types": touches,
                "duplicate": duplicate,
                "merged_into_opportunity_id": (
                    str(opportunity["merged_into_opportunity_id"])
                    if opportunity.get("merged_into_opportunity_id")
                    else None
                ),
                "warning": warning,
            }
        )

    published = [item for item in items if item["publication_status"] == "PUBLISHED"]
    customer_candidates = [
        item
        for item in items
        if item["category"] == "VALID_SUPPORTED"
        and item["publication_status"] == "DRAFT"
        and (item.get("postcode") or item.get("town"))
        and item["change_type"] in {"OPENING", "EXPANSION"}
        and not item["warning"]
    ][:25]
    cleanup_preview = {
        "potential_retire": category_counts["UNSUPPORTED_ORPHAN_CANDIDATE"],
        "potential_supersede": category_counts["SUPERSEDED_CANDIDATE"],
        "potential_merge": category_counts["DUPLICATE_CANDIDATE"],
        "preserve_for_human_review": (
            category_counts["MANUAL_OR_ADMIN_TOUCHED_PRESERVE"]
            + category_counts["NEEDS_INVESTIGATION"]
        ),
    }
    return {
        "total": len(items),
        "category_counts": {key: category_counts[key] for key in HYGIENE_CATEGORIES},
        "publication_counts": dict(sorted(publication_counts.items())),
        "change_type_counts": dict(sorted(change_type_counts.items())),
        "source_mix_counts": dict(sorted(source_mix_counts.items())),
        "orphan_root_causes": dict(sorted(root_causes.items())),
        "admin_touch_counts": dict(sorted(touch_counts.items())),
        "published": published,
        "published_warnings": [item for item in published if item["warning"]],
        "duplicate_candidates": [
            item for item in items if item["category"] == "DUPLICATE_CANDIDATE"
        ],
        "superseded_candidates": [
            item for item in items if item["category"] == "SUPERSEDED_CANDIDATE"
        ],
        "orphan_candidates": [
            item for item in items if item["category"] == "UNSUPPORTED_ORPHAN_CANDIDATE"
        ],
        "needs_investigation": [
            item for item in items if item["category"] == "NEEDS_INVESTIGATION"
        ],
        "customer_readiness_candidates": customer_candidates,
        "cleanup_preview": cleanup_preview,
        "items": items,
        "read_only": True,
    }
