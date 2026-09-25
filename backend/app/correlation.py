from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CorrelationMatch:
    matched: bool
    reason: str


def normalize_identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def compatible_names(left: Any, right: Any) -> bool:
    ignored = {"nursery", "day", "the", "ltd", "limited", "childcare"}
    a = set(normalize_identity(left).split()) - ignored
    b = set(normalize_identity(right).split()) - ignored
    return bool(a and b and a & b)


def deterministic_match(
    postcode: Any, name: Any, other_postcode: Any, other_name: Any
) -> CorrelationMatch:
    if not postcode or not other_postcode:
        return CorrelationMatch(False, "postcode missing")
    if normalize_identity(postcode) != normalize_identity(other_postcode):
        return CorrelationMatch(False, "postcode differs")
    if not compatible_names(name, other_name):
        return CorrelationMatch(False, "postcode matches but operator/nursery name is incompatible")
    return CorrelationMatch(True, "same postcode and compatible operator/nursery name")
