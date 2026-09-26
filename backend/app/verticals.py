from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NURSERY = "NURSERY"
CHILDRENS_HOME = "CHILDRENS_HOME"
DENTAL = "DENTAL"
ALL_VERTICALS = "ALL"


@dataclass(frozen=True)
class VerticalDefinition:
    key: str
    display_name: str
    enabled: bool
    policy: str


VERTICAL_REGISTRY = {
    NURSERY: VerticalDefinition(NURSERY, "NurserySignal", True, "nursery"),
    CHILDRENS_HOME: VerticalDefinition(CHILDRENS_HOME, "CareSignal", False, "children-home"),
    DENTAL: VerticalDefinition(DENTAL, "DentalSignal", False, "dental"),
}


def validate_vertical(value: str | None, *, allow_all: bool = False) -> str:
    vertical = str(value or NURSERY).upper()
    if allow_all and vertical == ALL_VERTICALS:
        return vertical
    if vertical not in VERTICAL_REGISTRY:
        raise ValueError("unsupported vertical")
    if not VERTICAL_REGISTRY[vertical].enabled:
        raise ValueError("vertical is not enabled")
    return vertical


def registry_payload() -> list[dict[str, Any]]:
    return [
        {"key": item.key, "display_name": item.display_name, "enabled": item.enabled}
        for item in VERTICAL_REGISTRY.values()
    ]


class NurseryVerticalPolicy:
    """Adapter boundary for existing NurserySignal behaviour."""

    key = NURSERY

    @staticmethod
    def classify_signal(raw: dict[str, Any]) -> dict[str, Any]:
        from app.enrichment import fixture_enrichment

        return fixture_enrichment(raw)

    @staticmethod
    def determine_opportunity_action(candidate: dict[str, Any]) -> Any:
        from app.opportunity_policy import opportunity_creation_decision

        return opportunity_creation_decision(candidate)


VERTICAL_POLICIES = {NURSERY: NurseryVerticalPolicy()}


def policy_for(vertical: str) -> NurseryVerticalPolicy:
    validated = validate_vertical(vertical)
    try:
        return VERTICAL_POLICIES[validated]
    except KeyError as exc:
        raise ValueError("no policy is configured for vertical") from exc
