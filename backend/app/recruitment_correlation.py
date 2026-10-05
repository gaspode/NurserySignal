"""Conservative, explainable recruitment-to-opportunity correlation.

Recruitment is corroborating evidence.  It must never create an opportunity by
itself, and a shared postcode is deliberately not enough to attach it to one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.correlation import compatible_names, normalize_identity

POLICY_VERSION = "recruitment-correlation-v1"


@dataclass(frozen=True)
class RecruitmentMatch:
    outcome: str
    confidence: float
    reason_codes: tuple[str, ...]

    @property
    def reason(self) -> str:
        return "; ".join(self.reason_codes)


def _value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def _specific(value: str) -> bool:
    normalized = normalize_identity(value)
    return len(normalized.split()) >= 2 and normalized not in {
        "new nursery", "nursery setting", "childrens home", "children home", "care home"
    }


def classify_recruitment_match(
    signal: dict[str, Any], opportunity: dict[str, Any]
) -> RecruitmentMatch:
    """Compare operator and site independently; never link on postcode alone."""
    facts = signal.get("extracted_facts") or {}
    metadata = signal.get("metadata") or {}
    signal_operator = _value(signal, "operator_name", "organisation_hint") or _value(
        facts, "operator_name"
    )
    opportunity_operator = _value(opportunity, "operator_name", "organisation_name")
    signal_address = _value(signal, "address", "location_hint") or _value(facts, "address")
    opportunity_address = _value(opportunity, "address")
    signal_site = _value(signal, "nursery_name", "site_name") or _value(facts, "nursery_name")
    opportunity_site = _value(opportunity, "site_name", "name")
    signal_postcode = _value(metadata, "postcode") or _value(signal, "postcode")
    opportunity_postcode = _value(opportunity, "postcode")
    signal_town = _value(metadata, "town", "locality") or _value(signal, "town")
    opportunity_town = _value(opportunity, "town")

    codes: list[str] = []
    operator_exact = bool(
        signal_operator
        and opportunity_operator
        and normalize_identity(signal_operator) == normalize_identity(opportunity_operator)
    )
    if operator_exact:
        codes.append("organisation_exact")
    elif (
        signal_operator
        and opportunity_operator
        and not compatible_names(signal_operator, opportunity_operator)
    ):
        codes.append("conflicting_operator")
        return RecruitmentMatch("NO_MATCH", 0.0, tuple(codes))

    address_exact = bool(
        signal_address
        and opportunity_address
        and normalize_identity(signal_address) == normalize_identity(opportunity_address)
    )
    if address_exact:
        codes.append("address_exact")
    site_exact = bool(
        _specific(signal_site)
        and _specific(opportunity_site)
        and normalize_identity(signal_site) == normalize_identity(opportunity_site)
    )
    if site_exact:
        codes.append("site_name_exact")
    postcode_exact = bool(
        signal_postcode
        and opportunity_postcode
        and normalize_identity(signal_postcode) == normalize_identity(opportunity_postcode)
    )
    if postcode_exact:
        codes.append("postcode_exact")
    town_match = bool(
        signal_town
        and opportunity_town
        and normalize_identity(signal_town) == normalize_identity(opportunity_town)
    )
    if town_match:
        codes.append("town_match")

    # A precise site is enough; otherwise an explicit organisation plus the
    # same postcode is the minimum auto-link standard.
    if (address_exact or site_exact) and (postcode_exact or operator_exact):
        return RecruitmentMatch("EXACT", 0.98, tuple(codes))
    if operator_exact and postcode_exact:
        return RecruitmentMatch("STRONG", 0.92, tuple(codes))
    if (address_exact or site_exact) and town_match:
        return RecruitmentMatch("PROBABLE", 0.76, tuple(codes))
    if postcode_exact:
        return RecruitmentMatch(
            "UNCERTAIN", 0.45, tuple(codes + ["postcode_without_corroboration"])
        )
    if operator_exact and town_match:
        return RecruitmentMatch("UNCERTAIN", 0.5, tuple(codes + ["site_not_identified"]))
    return RecruitmentMatch("NO_MATCH", 0.0, tuple(codes or ["insufficient_identity"]))
