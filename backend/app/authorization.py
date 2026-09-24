from __future__ import annotations

import json
from typing import Any


def normalized_groups(value: Any) -> set[str]:
    """Return exact Cognito group names from common API Gateway claim shapes."""

    if isinstance(value, list):
        return {item.strip() for item in value if isinstance(item, str) and item.strip()}
    if not isinstance(value, str):
        return set()

    raw = value.strip()
    if not raw:
        return set()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return {item.strip() for item in parsed if isinstance(item, str) and item.strip()}
    if isinstance(parsed, str) and parsed.strip():
        return {parsed.strip()}

    return {item.strip() for item in raw.split(",") if item.strip()}
