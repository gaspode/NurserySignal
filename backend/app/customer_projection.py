from __future__ import annotations

import re
from typing import Any

FULL_UK_POSTCODE_RE = re.compile(
    r"\b(?:GIR\s?0AA|[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]?\s*\d[ABD-HJLNP-UW-Z]{2})\b",
    re.IGNORECASE,
)
OUTWARD_POSTCODE_RE = re.compile(
    r"^(?:GIR|[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]?)$",
    re.IGNORECASE,
)
STREET_ADDRESS_RE = re.compile(
    r"^\s*\d+[A-Z]?(?:[-/]\d+[A-Z]?)?\s+.*\b"
    r"(?:ROAD|STREET|LANE|CLOSE|AVENUE|DRIVE|WAY|CRESCENT|PLACE|GARDENS|COURT)\b",
    re.IGNORECASE,
)
AUTHORITY_SUFFIX_RE = re.compile(
    r"\s+(?:METROPOLITAN\s+BOROUGH|BOROUGH|DISTRICT|CITY|COUNTY)?\s*COUNCIL$",
    re.IGNORECASE,
)


def postcode_district(value: Any) -> str | None:
    """Return a valid UK outward postcode without exposing the inward unit."""
    normalized = " ".join(str(value or "").upper().split()).strip()
    if not normalized:
        return None
    if OUTWARD_POSTCODE_RE.fullmatch(normalized):
        return normalized
    match = FULL_UK_POSTCODE_RE.fullmatch(normalized)
    if not match:
        return None
    return normalized.split(" ", 1)[0] if " " in normalized else normalized[:-3]


def _safe_place(value: Any, *, authority: bool = False) -> str | None:
    place = " ".join(str(value or "").replace(",", " ").split()).strip(" -")
    if not place or STREET_ADDRESS_RE.search(place):
        return None
    place = FULL_UK_POSTCODE_RE.sub("", place).strip(" ,-—")
    if authority:
        place = AUTHORITY_SUFFIX_RE.sub("", place).strip()
    if not place or any(character.isdigit() for character in place) or len(place) > 80:
        return None
    return place


def customer_safe_place(value: Any, *, authority: bool = False) -> str | None:
    """Project one coarse place label without accepting address-like values."""
    return _safe_place(value, authority=authority)


def customer_safe_location(
    *,
    town: Any = None,
    local_authority: Any = None,
    region: Any = None,
    postcode: Any = None,
) -> str | None:
    """Build coarse site geography; caller must not pass provider-office fields."""
    locality = (
        _safe_place(town) or _safe_place(local_authority, authority=True) or _safe_place(region)
    )
    district = postcode_district(postcode)
    if locality and district and district.casefold() not in locality.casefold():
        return f"{locality}, {district}"
    return locality or district


def generated_customer_title(row: dict[str, Any]) -> str:
    location = customer_safe_location(
        town=row.get("town"),
        local_authority=row.get("local_authority"),
        region=row.get("region"),
        postcode=row.get("postcode"),
    )
    subject = {
        "OPENING": "New children’s home",
        "EXPANSION": "Children’s home expansion",
        "RELOCATION": "Children’s home relocation",
        "OTHER_CHANGE": "Children’s home development",
    }.get(str(row.get("change_type") or ""), "Children’s home development")
    return f"{subject} — {location}" if location else subject


def customer_title(row: dict[str, Any]) -> str:
    override = str(row.get("customer_title") or "").strip()
    return override or generated_customer_title(row)


def generated_customer_summary(row: dict[str, Any]) -> str:
    sources = set(row.get("source_types") or [])
    change = str(row.get("change_type") or "OTHER_CHANGE")
    lifecycle = str(
        row.get("customer_lifecycle_stage")
        or row.get("derived_customer_lifecycle")
        or row.get("lifecycle_stage")
        or ""
    )
    if row.get("foundational_evidence") is False:
        return (
            "Current reviewed evidence concerns existing or supporting context and does "
            "not establish a new children’s-home opening."
        )
    if lifecycle == "PLANNING_PENDING":
        return (
            "A planning application has been submitted for material children’s-home "
            "provision and is awaiting a decision."
        )
    if lifecycle == "PLANNING_APPROVED":
        return "Planning permission has been approved for material children’s-home provision."
    if lifecycle == "APPEAL_PENDING":
        return "A planning appeal is in progress for proposed children’s-home provision."
    if lifecycle == "NEEDS_REVIEW":
        return (
            "The current status of this children’s-home opportunity is under review. "
            "Reviewed public evidence is available for context."
        )
    if lifecycle == "DELIVERY_SIGNAL_DETECTED":
        return "Reviewed recruitment evidence indicates mobilisation for this opportunity."
    if lifecycle in {"REGISTRATION_DETECTED", "REGISTERED"}:
        return "Official Ofsted evidence records regulatory progress for this opportunity."
    if len(sources) >= 2:
        return "Multiple independent public sources support this opportunity."
    if "planning" in sources:
        if change == "EXPANSION":
            return "A planning application explicitly indicates increased children’s-home capacity."
        return "A planning application explicitly proposes material children’s-home provision."
    if "recruitment" in sources:
        return (
            "Recruitment evidence explicitly refers to a new or materially changing "
            "children’s home."
        )
    if "ofsted" in sources:
        return "Official Ofsted evidence confirms regulatory progress."
    return "Reviewed public evidence supports a material children’s-home change."


def customer_summary(row: dict[str, Any]) -> str:
    override = str(row.get("customer_summary") or "").strip()
    return override or generated_customer_summary(row)


def safe_evidence_title(value: Any) -> str | None:
    title = " ".join(str(value or "").split()).strip()
    if not title or FULL_UK_POSTCODE_RE.search(title) or STREET_ADDRESS_RE.search(title):
        return None
    return title[:120]
