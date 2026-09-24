from __future__ import annotations

import json
from typing import Any


def _clean_group(value: str) -> str:
    group = value.strip()
    if len(group) >= 2 and group[0] == group[-1] and group[0] in {'"', "'"}:
        group = group[1:-1].strip()
    return group


def _groups_from_string(value: str) -> set[str]:
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    if not raw:
        return set()
    return {group for group in (_clean_group(item) for item in raw.split(",")) if group}


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
        return _groups_from_string(parsed)

    return _groups_from_string(raw)
