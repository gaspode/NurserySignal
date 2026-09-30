from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.care_planning_review import extract_prior_planning_references
from app.correlation import normalize_identity
from app.planning_outcomes import PlanningOutcome, canonical_planning_outcome

PLANNING_FAMILY_POLICY_VERSION = "planning-family-v1"

FOUNDATIONAL_SUBTYPES = frozenset(
    {
        "NEW_HOME_CHANGE_OF_USE",
        "NEW_HOME_OTHER_EXPLICIT",
        "NEW_HOME_MIXED_USE",
        "LAWFULNESS_PROPOSED",
        "EXPANSION_OR_CAPACITY_CHANGE",
    }
)
SUPPORT_ONLY_SUBTYPES = frozenset(
    {
        "CONDITION_VARIATION",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
    }
)
NON_POSITIVE_SUBTYPES = frozenset(
    {"LAWFULNESS_EXISTING", "CESSATION_OR_CHANGE_AWAY_FROM_CARE", "REFUSED", "WITHDRAWN"}
)

_REFERENCE_VALUE = re.compile(r"^[A-Z0-9][A-Z0-9/._-]{3,39}$")


def normalize_planning_reference(value: Any) -> str | None:
    """Normalize harmless formatting without collapsing meaningful reference syntax."""
    text = str(value or "").strip().upper()
    text = text.strip(".,;:()[]{}")
    text = re.sub(r"\s*([/._-])\s*", r"\1", text)
    text = re.sub(r"\s+", "", text)
    if not text or not any(character.isdigit() for character in text):
        return None
    return text if _REFERENCE_VALUE.fullmatch(text) else None


def normalize_planning_authority(value: Any) -> str | None:
    normalized = normalize_identity(value)
    return normalized or None


def planning_authority(metadata: Any) -> str | None:
    data = metadata if isinstance(metadata, dict) else {}
    provider = data.get("provider_record")
    provider = provider if isinstance(provider, dict) else {}
    authority = data.get("authority")
    authority = authority if isinstance(authority, dict) else {}
    value = (
        data.get("council")
        or data.get("local_authority")
        or authority.get("name")
        or provider.get("council")
        or provider.get("authority_name")
    )
    return str(value).strip() if value else None


def primary_planning_reference(external_id: Any, metadata: Any) -> str | None:
    data = metadata if isinstance(metadata, dict) else {}
    provider = data.get("provider_record")
    provider = provider if isinstance(provider, dict) else {}
    candidates = (
        data.get("planning_reference"),
        data.get("application_reference"),
        data.get("application_id"),
        provider.get("reference"),
        provider.get("application_reference"),
        data.get("provider_application_id"),
        provider.get("id"),
        str(external_id or "").removeprefix("plota:").removeprefix("PLOTA:"),
    )
    for candidate in candidates:
        normalized = normalize_planning_reference(candidate)
        if normalized:
            return normalized
    return None


def prior_planning_references(
    raw: dict[str, Any], facts: Any = None
) -> tuple[tuple[str, str], ...]:
    extracted = facts if isinstance(facts, dict) else {}
    values = extracted.get("planning_prior_references")
    raw_values = values if isinstance(values, list) else extract_prior_planning_references(
        "\n".join((str(raw.get("title") or ""), str(raw.get("raw_text") or "")))
    )
    result: list[tuple[str, str]] = []
    for value in raw_values:
        normalized = normalize_planning_reference(value)
        raw_value = str(value).strip()
        if normalized and all(item[1] != normalized for item in result):
            result.append((raw_value, normalized))
    return tuple(result)


def is_foundational_planning_signal(subtype: Any, metadata: Any) -> bool:
    outcome = canonical_planning_outcome(metadata).outcome
    return str(subtype or "") in FOUNDATIONAL_SUBTYPES and outcome not in {
        PlanningOutcome.REFUSED,
        PlanningOutcome.WITHDRAWN,
        PlanningOutcome.REFUSED_UNDER_APPEAL,
        PlanningOutcome.APPEAL_DISMISSED,
    }


def followup_can_support_opportunity(subtype: Any, metadata: Any) -> bool:
    outcome = canonical_planning_outcome(metadata).outcome
    return str(subtype or "") in SUPPORT_ONLY_SUBTYPES and outcome not in {
        PlanningOutcome.REFUSED,
        PlanningOutcome.WITHDRAWN,
        PlanningOutcome.REFUSED_UNDER_APPEAL,
        PlanningOutcome.APPEAL_DISMISSED,
    }


@dataclass(frozen=True)
class PlanningFamilyIdentity:
    authority: str
    normalized_authority: str
    raw_reference: str
    normalized_reference: str

    @classmethod
    def create(cls, authority: Any, raw_reference: Any) -> PlanningFamilyIdentity | None:
        authority_text = str(authority or "").strip()
        normalized_authority = normalize_planning_authority(authority_text)
        reference_text = str(raw_reference or "").strip()
        normalized_reference = normalize_planning_reference(reference_text)
        if not authority_text or not normalized_authority or not normalized_reference:
            return None
        return cls(authority_text, normalized_authority, reference_text, normalized_reference)
