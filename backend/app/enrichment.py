from __future__ import annotations

from datetime import date
from typing import Any

from app.classification import CLASSIFICATION_RULE_VERSION, classify_signal_text
from app.planning import candidate_decision, planning_record_from_signal


def fixture_enrichment(
    raw: dict[str, Any], *, planning_candidate: Any | None = None
) -> dict[str, Any]:
    classification_result = classify_signal_text(
        (
            raw.get("title"),
            raw.get("raw_text"),
            raw.get("location_hint"),
            raw.get("organisation_hint"),
            raw.get("metadata"),
        )
    )
    text = classification_result.text
    source_type = str(raw["source_type"]).lower()
    if planning_candidate is None and source_type == "planning":
        try:
            planning_candidate = candidate_decision(planning_record_from_signal(raw))
        except (KeyError, TypeError, ValueError):
            # Older/manual planning fixtures may not contain provider metadata.
            # They retain the original fixture behaviour rather than failing
            # enrichment solely because they predate the planning adapter.
            planning_candidate = None

    planning_excluded = planning_candidate is not None and not planning_candidate.matched
    school_nursery = planning_candidate is not None and planning_candidate.school_nursery
    if classification_result.likely_false_positive or planning_excluded:
        event_type, lifecycle_stage, confidence = "other", "DISCOVERED", 0.2
        classification = (
            "horticultural-nursery"
            if classification_result.likely_false_positive
            else "planning-excluded"
        )
    elif "planning" in source_type or any(
        word in text for word in ("planning", "application", "proposed")
    ):
        confidence = 0.72 if school_nursery else 0.86
        event_type, lifecycle_stage = "opening", "PLANNING"
        classification = "school-nursery" if school_nursery else "planning-opening"
    elif any(word in text for word in ("recruit", "vacancy", "room leader", "staff")):
        event_type, lifecycle_stage, confidence = "opening", "RECRUITING", 0.76
        classification = "recruitment-opening"
    elif any(word in text for word in ("announce", "opening", "new nursery", "chain")):
        event_type, lifecycle_stage, confidence = "opening", "OPENING_SOON", 0.8
        classification = "operator-announcement"
    else:
        event_type, lifecycle_stage, confidence = "other", "DISCOVERED", 0.2
        classification = "irrelevant-or-unclear"

    metadata = raw.get("metadata") or {}
    expected_date = metadata.get("expected_opening_date")
    if expected_date:
        date.fromisoformat(str(expected_date))
    capacity = metadata.get("capacity")
    if capacity is not None:
        capacity = int(capacity)
    nursery_name = raw.get("organisation_hint") or raw["title"]
    return {
        "raw_signal_id": raw["id"],
        "schema_version": "1.0",
        "event_type": event_type,
        "nursery_name": nursery_name,
        "operator_name": raw.get("organisation_hint"),
        "address": raw.get("location_hint"),
        "expected_opening_date": expected_date,
        "capacity": capacity,
        "lifecycle_stage": lifecycle_stage,
        "confidence": confidence,
        "extracted_facts": {
            "method": "fixture-v1",
            "classification": classification,
            "classification_rule_version": CLASSIFICATION_RULE_VERSION,
            "childcare_terms": list(classification_result.childcare_terms),
            "horticultural_terms": list(classification_result.horticultural_terms),
            "likely_false_positive": (
                classification_result.likely_false_positive or planning_excluded
            ),
            "planning_candidate_matched": (
                planning_candidate.matched if planning_candidate is not None else None
            ),
            "planning_positive_terms": (
                list(planning_candidate.positive_terms) if planning_candidate is not None else []
            ),
            "planning_exclusions": (
                list(planning_candidate.exclusions) if planning_candidate is not None else []
            ),
            "school_nursery": school_nursery,
            "source_type": raw["source_type"],
        },
        "evidence": {
            "source_url": raw["source_url"],
            "title": raw["title"],
            "location_hint": raw.get("location_hint"),
        },
    }
