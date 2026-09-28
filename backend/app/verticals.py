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
    recruitment_routes: tuple[str, ...] = ()


VERTICAL_REGISTRY = {
    NURSERY: VerticalDefinition(
        NURSERY, "NurserySignal", True, "nursery", ("Education and early years",)
    ),
    CHILDRENS_HOME: VerticalDefinition(
        CHILDRENS_HOME, "CareProspect", True, "children-home", ("Care services",)
    ),
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


def validate_vertical_filter(value: str | None) -> str:
    """Validate an admin list scope; omitted scopes remain NurserySignal-safe."""
    return validate_vertical(value or NURSERY, allow_all=True)


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

    @staticmethod
    def build_opportunity_title(candidate: dict[str, Any], decision: Any) -> str:
        from app.opportunity_policy import opportunity_title

        return opportunity_title(candidate, decision)

    @staticmethod
    def initial_lifecycle(source_type: str) -> str:
        return "PLANNING" if source_type == "planning" else "DISCOVERED"


class ChildrenHomeVerticalPolicy:
    """Children's-home interpretation behind the shared SignalHub boundary."""

    key = CHILDRENS_HOME

    @staticmethod
    def classify_signal(raw: dict[str, Any]) -> dict[str, Any]:
        from app.care import enrich_care_signal

        return enrich_care_signal(raw)

    @staticmethod
    def determine_opportunity_action(candidate: dict[str, Any]) -> Any:
        from app.care import care_opportunity_decision

        return care_opportunity_decision(candidate)

    @staticmethod
    def build_opportunity_title(candidate: dict[str, Any], decision: Any) -> str:
        from app.care import care_opportunity_title

        return care_opportunity_title(candidate, decision)

    @staticmethod
    def initial_lifecycle(source_type: str) -> str:
        return {
            "planning": "PLANNING",
            "recruitment": "RECRUITING",
            "ofsted": "REGISTRATION",
            "procurement": "DISCOVERED",
        }.get(source_type, "DISCOVERED")


VERTICAL_POLICIES = {
    NURSERY: NurseryVerticalPolicy(),
    CHILDRENS_HOME: ChildrenHomeVerticalPolicy(),
}


def policy_for(vertical: str) -> NurseryVerticalPolicy | ChildrenHomeVerticalPolicy:
    validated = validate_vertical(vertical)
    try:
        return VERTICAL_POLICIES[validated]
    except KeyError as exc:
        raise ValueError("no policy is configured for vertical") from exc
