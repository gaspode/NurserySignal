from __future__ import annotations

import json
from typing import Any

from app.care import (
    care_planning_signal,
    care_recruitment_signal,
    classify_care_planning,
    classify_care_recruitment,
)
from app.config import Settings
from app.ingestion import NormalizedSignal
from app.planning import planning_record_from_signal
from app.recruitment import recruitment_record_from_signal
from app.repository import list_vertical_backfill_candidates, record_admin_audit
from app.service import ingest_signal


def backfill_care_from_stored_evidence(
    settings: Settings, *, actor: str, days: int = 60, limit: int = 25
) -> dict[str, Any]:
    """Boundedly derive CareSignal records from preserved source evidence."""
    days = min(max(int(days), 1), 90)
    limit = min(max(int(limit), 1), 50)
    rows = list_vertical_backfill_candidates(
        settings,
        from_vertical="NURSERY",
        days=days,
        limit=limit,
        recruitment_discovery_vertical="CHILDRENS_HOME",
    )
    counts = {
        "evaluated": len(rows),
        "relevant": 0,
        "accepted": 0,
        "duplicates": 0,
        "routine_supporting": 0,
        "change_signals": 0,
        "irrelevant": 0,
        "errors": 0,
        "planning_evaluated": sum(row["source_type"] == "planning" for row in rows),
        "recruitment_evaluated": sum(row["source_type"] == "recruitment" for row in rows),
    }
    signal_ids: list[str] = []
    for raw in rows:
        try:
            if raw["source_type"] == "planning":
                decision = classify_care_planning(planning_record_from_signal(raw))
                if not decision.matched:
                    counts["irrelevant"] += 1
                    continue
                signal_payload = care_planning_signal(planning_record_from_signal(raw), decision)
                counts["change_signals"] += 1
            else:
                record = recruitment_record_from_signal(raw)
                decision = classify_care_recruitment(record)
                if not decision["matched"]:
                    counts["irrelevant"] += 1
                    continue
                signal_payload = care_recruitment_signal(record, decision)
                if decision["relevance"] == "RELEVANT_CHANGE":
                    counts["change_signals"] += 1
                else:
                    counts["routine_supporting"] += 1
            signal_payload["metadata"]["backfilled_from_signal_id"] = str(raw["id"])
            counts["relevant"] += 1
            payload = json.dumps(signal_payload, separators=(",", ":"), default=str).encode()
            result = ingest_signal(settings, NormalizedSignal.from_dict(signal_payload), payload)
            counts["duplicates" if result.status == "duplicate" else "accepted"] += 1
            signal_ids.append(result.signal_id)
        except Exception:
            counts["errors"] += 1
    operation_id = record_admin_audit(
        settings,
        action="care_stored_evidence_backfill",
        actor=actor,
        target_type="vertical",
        details={
            "vertical": "CHILDRENS_HOME",
            "days": days,
            "limit": limit,
            "counts": counts,
            "signal_ids": signal_ids,
        },
    )
    return {"operation_id": operation_id, "days": days, "limit": limit, **counts}
