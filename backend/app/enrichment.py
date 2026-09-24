from __future__ import annotations

from datetime import date
from typing import Any


def fixture_enrichment(raw: dict[str, Any]) -> dict[str, Any]:
    text = f"{raw['title']} {raw['raw_text']}".lower()
    source_type = str(raw["source_type"]).lower()
    if "planning" in source_type or any(
        word in text for word in ("planning", "application", "proposed")
    ):
        event_type, lifecycle_stage, confidence = "opening", "PLANNING", 0.86
        classification = "planning-opening"
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
            "source_type": raw["source_type"],
        },
        "evidence": {
            "source_url": raw["source_url"],
            "title": raw["title"],
            "location_hint": raw.get("location_hint"),
        },
    }
