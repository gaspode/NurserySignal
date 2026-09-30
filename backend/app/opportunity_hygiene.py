from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from app.evidence_support import EvidenceSupport, classify_evidence_support
from app.planning_outcomes import PlanningOutcome, canonical_planning_outcome

HYGIENE_CATEGORIES = (
    "VALID_SUPPORTED",
    "DUPLICATE_CANDIDATE",
    "SUPERSEDED_CANDIDATE",
    "UNSUPPORTED_ORPHAN_CANDIDATE",
    "MANUAL_OR_ADMIN_TOUCHED_PRESERVE",
    "NEEDS_INVESTIGATION",
)

ORPHAN_ROOT_CAUSES = (
    "PLANNING_REFUSED",
    "PLANNING_WITHDRAWN",
    "SIGNAL_REJECTED",
    "RELATIONSHIP_REMOVED",
    "TAXONOMY_RECLASSIFIED",
    "OLD_CREATION_RULE",
    "DUPLICATE_SHELL",
    "NO_ACTIVE_SUPPORT",
    "OTHER",
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
    if opportunity.get("review_status") == "MERGED" or opportunity.get(
        "merged_into_opportunity_id"
    ):
        touches.add("merge")
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
        if action == "opportunity_semantic_drift_corrected":
            # This bounded system correction is provenance, not evidence that a
            # human deliberately curated or overrode the opportunity.
            continue
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


def opportunity_admin_touch_types(opportunity: dict[str, Any]) -> list[str]:
    """Expose the audit's conservative manual/admin-touch semantics."""
    return _touch_types(opportunity)


def legacy_relation_support_state(relation: dict[str, Any]) -> str:
    """Pre-shared-classifier semantics retained only for before/after diagnostics."""
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


def _relation_state(relation: dict[str, Any]) -> str:
    state = classify_evidence_support(relation)
    if state is EvidenceSupport.OTHER_LIFECYCLE:
        outcome = canonical_planning_outcome(relation.get("metadata")).outcome
        if outcome in NEGATIVE_OUTCOMES or outcome is PlanningOutcome.REFUSED_UNDER_APPEAL:
            return outcome.value
    return {
        EvidenceSupport.FOUNDATIONAL: "FOUNDATIONAL",
        EvidenceSupport.SUPPORTING_FOLLOWUP: "SUPPORT_ONLY",
        EvidenceSupport.OTHER_LIFECYCLE: "OTHER_LIFECYCLE",
        EvidenceSupport.NON_SUPPORTING: (
            "PENDING"
            if relation.get("status") == "ACTIVE" and relation.get("review_status") == "PENDING"
            else "REJECTED"
            if relation.get("status") == "ACTIVE"
            and relation.get("review_status") == "REJECTED"
            else "INACTIVE"
        ),
    }[state]


def _duplicate_counterparts(
    opportunities: list[dict[str, Any]],
    relation_state: Callable[[dict[str, Any]], str],
) -> dict[str, dict[str, Any]]:
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
                sum(relation_state(rel) == "FOUNDATIONAL" for rel in item["relationships"]),
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


def audit_opportunities(
    opportunities: list[dict[str, Any]], *, legacy_support_semantics: bool = False
) -> dict[str, Any]:
    """Classify a complete vertical inventory without changing any stored state."""
    relation_state = legacy_relation_support_state if legacy_support_semantics else _relation_state
    duplicates = _duplicate_counterparts(opportunities, relation_state)
    items: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    root_causes: Counter[str] = Counter()
    touch_counts: Counter[str] = Counter()
    source_mix_counts: Counter[str] = Counter()
    publication_counts: Counter[str] = Counter()
    change_type_counts: Counter[str] = Counter()
    category_by_publication: dict[str, Counter[str]] = defaultdict(Counter)
    category_by_change_type: dict[str, Counter[str]] = defaultdict(Counter)
    category_by_source_mix: dict[str, Counter[str]] = defaultdict(Counter)

    for opportunity in opportunities:
        opportunity_id = str(opportunity["id"])
        relationships = opportunity.get("relationships") or []
        states = [relation_state(relation) for relation in relationships]
        foundational = sum(state == "FOUNDATIONAL" for state in states)
        supporting_followups = sum(state == "SUPPORT_ONLY" for state in states)
        other_lifecycle = sum(
            state
            in {
                "OTHER_LIFECYCLE",
                "REFUSED",
                "WITHDRAWN",
                "REFUSED_UNDER_APPEAL",
                "APPEAL_DISMISSED",
            }
            for state in states
        )
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
        category_by_publication[category][
            str(opportunity.get("publication_status") or "UNKNOWN")
        ] += 1
        category_by_change_type[category][
            str(opportunity.get("change_type") or "UNKNOWN")
        ] += 1
        category_by_source_mix[category][source_mix] += 1
        hygiene_reason = {
            "VALID_SUPPORTED": "Current foundational evidence supports this opportunity.",
            "DUPLICATE_CANDIDATE": (
                duplicate.get("reason") if duplicate else "Potential duplicate opportunity."
            ),
            "SUPERSEDED_CANDIDATE": "Opportunity belongs to an existing supersession chain.",
            "UNSUPPORTED_ORPHAN_CANDIDATE": root_cause.replace("_", " ").title()
            if root_cause
            else "No defensible active support remains.",
            "MANUAL_OR_ADMIN_TOUCHED_PRESERVE": (
                f"Preserved because of admin history: {', '.join(touches)}."
            ),
            "NEEDS_INVESTIGATION": warning,
        }[category]
        items.append(
            {
                "opportunity_id": opportunity_id,
                "name": opportunity.get("name"),
                "review_status": opportunity.get("review_status"),
                "publication_status": opportunity.get("publication_status"),
                "lifecycle_stage": opportunity.get("lifecycle_stage"),
                "change_type": opportunity.get("change_type"),
                "confidence": opportunity.get("confidence"),
                "operator_name": opportunity.get("operator_name"),
                "postcode": opportunity.get("postcode"),
                "town": opportunity.get("town"),
                "creation_reason": opportunity.get("creation_reason"),
                "stage_reason": opportunity.get("stage_reason"),
                "category": category,
                "root_cause": root_cause,
                "hygiene_reason": hygiene_reason,
                "supporting_signal_count": foundational,
                "foundational_signal_count": foundational,
                "supporting_followup_count": supporting_followups,
                "other_lifecycle_signal_count": other_lifecycle,
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
    all_customer_candidates = [
        item
        for item in items
        if item["category"] == "VALID_SUPPORTED"
        and item["publication_status"] == "DRAFT"
        and (item.get("postcode") or item.get("town"))
        and item["change_type"] in {"OPENING", "EXPANSION"}
        and not item["warning"]
    ]
    customer_candidate_ids = {
        item["opportunity_id"] for item in all_customer_candidates
    }
    for item in items:
        item["customer_readiness_candidate"] = (
            item["opportunity_id"] in customer_candidate_ids
        )
        item["customer_readiness_reason"] = (
            "Supported unpublished opening/change evidence with usable geography and no "
            "hygiene warning."
            if item["customer_readiness_candidate"]
            else None
        )
    customer_candidates = all_customer_candidates[:25]
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
        "category_by_publication": {
            category: dict(sorted(values.items()))
            for category, values in sorted(category_by_publication.items())
        },
        "category_by_change_type": {
            category: dict(sorted(values.items()))
            for category, values in sorted(category_by_change_type.items())
        },
        "category_by_source_mix": {
            category: dict(sorted(values.items()))
            for category, values in sorted(category_by_source_mix.items())
        },
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
        "customer_readiness_total": len(all_customer_candidates),
        "cleanup_preview": cleanup_preview,
        "items": items,
        "read_only": True,
    }


def filter_hygiene_items(
    items: list[dict[str, Any]],
    *,
    category: str | None = None,
    root_cause: str | None = None,
    change_type: str | None = None,
    publication_status: str | None = None,
    q: str = "",
    view: str = "inventory",
) -> list[dict[str, Any]]:
    """Apply bounded read-only admin filters to authoritative audit results."""
    attention_categories = {
        "NEEDS_INVESTIGATION",
        "UNSUPPORTED_ORPHAN_CANDIDATE",
        "DUPLICATE_CANDIDATE",
        "SUPERSEDED_CANDIDATE",
    }
    return [
        item
        for item in items
        if (view != "publication_candidates" or item["customer_readiness_candidate"])
        and (
            view != "needs_attention"
            or item["category"] in attention_categories
            or (
                item.get("publication_status") == "PUBLISHED"
                and bool(item.get("warning"))
            )
        )
        and (category is None or item["category"] == category)
        and (root_cause is None or item["root_cause"] == root_cause)
        and (change_type is None or item["change_type"] == change_type)
        and (
            publication_status is None
            or item["publication_status"] == publication_status
        )
        and (
            not q
            or q
            in " ".join(
                str(item.get(field) or "").lower()
                for field in (
                    "name",
                    "town",
                    "postcode",
                    "creation_reason",
                    "stage_reason",
                )
            )
        )
    ]
