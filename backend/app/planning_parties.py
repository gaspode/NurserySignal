"""Explicit, provider-independent Planning party provenance.

Planning applicants and agents have different commercial meanings.  This module
only reads fields whose source payload labels the role; it intentionally never
uses ``organisation_hint`` or description text as party evidence.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

PLANNING_PARTY_PROVENANCE_VERSION = "planning-party-provenance-v1"


def _text(value: Any) -> str | None:
    value = " ".join(str(value or "").split()).strip(" .,")
    return value or None


def _company_like(value: str | None) -> bool:
    return bool(
        value
        and re.search(
            r"\b(?:ltd|limited|plc|llp|cic|inc|company|co|group|holdings|trust|"
            r"foundation|association|partnership)\b",
            value,
            re.IGNORECASE,
        )
    )


def _role_value(
    metadata: dict[str, Any],
    provider: dict[str, Any],
    *,
    role: str,
) -> tuple[Any, str | None]:
    keys = (role.casefold(), f"{role.casefold()}_name")
    for key in keys:
        if key in metadata and metadata.get(key) not in (None, ""):
            return metadata[key], f"metadata.{key}"
    for key in keys:
        if key in provider and provider.get(key) not in (None, ""):
            return provider[key], f"metadata.provider_record.{key}"
    return None, None


def _party(
    value: Any,
    source_path: str | None,
    metadata: dict[str, Any],
    provider: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any] | None:
    if value is None or source_path is None:
        return None
    embedded = value if isinstance(value, dict) else {}
    name = _text(
        embedded.get("name")
        or embedded.get("organisation_name")
        or embedded.get("company_name")
        or (value if not isinstance(value, dict) else None)
    )
    if not name:
        return None
    prefix = role.casefold()
    company_number = _text(
        embedded.get("company_number")
        or embedded.get("companies_house_number")
        or metadata.get(f"{prefix}_company_number")
        or provider.get(f"{prefix}_company_number")
    )
    party_type = _text(
        embedded.get("type")
        or metadata.get(f"{prefix}_type")
        or provider.get(f"{prefix}_type")
    )
    return {
        "role": role,
        "name": name,
        "party_type": party_type,
        "company_like": _company_like(name) or bool(company_number),
        "company_number": company_number,
        "source_path": source_path,
    }


def extract_planning_party_provenance(
    metadata: Any,
    *,
    raw_source_identifier: Any = None,
    extracted_at: datetime | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Extract only explicitly-labelled Planning parties from stored metadata.

    The returned reason is used for bounded historical-backfill reporting.
    """
    if not isinstance(metadata, dict):
        return None, "MALFORMED_OR_UNSUPPORTED_RAW"
    provider_value = metadata.get("provider_record")
    if provider_value is not None and not isinstance(provider_value, dict):
        return None, "MALFORMED_OR_UNSUPPORTED_RAW"
    provider = provider_value if isinstance(provider_value, dict) else {}
    applicant_value, applicant_path = _role_value(metadata, provider, role="APPLICANT")
    agent_value, agent_path = _role_value(metadata, provider, role="AGENT")
    applicant = _party(
        applicant_value, applicant_path, metadata, provider, role="APPLICANT"
    )
    agent = _party(agent_value, agent_path, metadata, provider, role="AGENT")
    if applicant and agent:
        reason = "APPLICANT_AND_AGENT"
    elif applicant:
        reason = (
            "APPLICANT_COMPANY_EXTRACTED"
            if applicant["company_like"]
            else "APPLICANT_PERSON_EXTRACTED"
        )
    elif agent:
        reason = "AGENT_ONLY"
    else:
        reason = "NO_ROLE_LABELLED_IDENTITY"
    extracted_at = extracted_at or datetime.now(UTC)
    return {
        "version": PLANNING_PARTY_PROVENANCE_VERSION,
        "raw_source_identifier": _text(raw_source_identifier),
        "extracted_at": extracted_at.isoformat(),
        "applicant": applicant,
        "agent": agent,
    }, reason
