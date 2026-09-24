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


def group_claim_shape(value: Any) -> str:
    """Describe a group claim without exposing its contents."""

    if value is None:
        return "missing"
    if isinstance(value, list):
        return "list"
    if not isinstance(value, str):
        return type(value).__name__
    raw = value.strip()
    if not raw:
        return "empty_string"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return "json_array_string"
    if isinstance(parsed, str):
        return "json_string"
    if "," in raw:
        return "comma_separated_string"
    return "string"
