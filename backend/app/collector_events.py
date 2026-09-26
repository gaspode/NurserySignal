from __future__ import annotations

import json
from typing import Any


def collector_payload(event: dict[str, Any] | None) -> dict[str, Any]:
    """Return a direct/EventBridge payload or one bounded SQS command payload."""
    event = event or {}
    records = event.get("Records")
    if records is None:
        return event
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("collector command must contain exactly one SQS record")
    record = records[0]
    if not isinstance(record, dict) or record.get("eventSource") != "aws:sqs":
        raise ValueError("collector command must be delivered by SQS")
    try:
        payload = json.loads(record.get("body") or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("collector command body is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("collector command body must be an object")
    return payload
