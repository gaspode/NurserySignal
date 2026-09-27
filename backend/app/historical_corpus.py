from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

CASE_LINK_CONFIDENCE = {
    "VERIFIED_CASE_LINK",
    "STRONG_CASE_LINK",
    "POSSIBLE_CASE_LINK",
    "REJECTED_CASE_LINK",
}
ELIGIBILITY_VALUES = {
    "ELIGIBLE",
    "EXCLUDED_AFTER_REGISTRATION",
    "EXCLUDED_NO_RELIABLE_PUBLICATION_DATE",
    "EXCLUDED_WEAK_CASE_LINK",
    "EXCLUDED_UNTRUSTWORTHY_SOURCE",
    "EXCLUDED_FUTURE_INFORMATION",
}
NOT_FOUND_REASONS = {
    "NOT_FOUND",
    "SOURCE_NOT_HISTORICALLY_SEARCHABLE",
    "FOUND_BUT_NOT_RELIABLY_DATED",
    "FOUND_BUT_CASE_LINK_UNCERTAIN",
}
TRUSTED_SOURCE_SUFFIXES = (
    ".gov.uk",
    ".gov.wales",
    "plota.co.uk",
    "api.plota.co.uk",
    "apprenticeships.education.gov.uk",
    "mansfield.moderngov.co.uk",
    "wigan.moderngov.co.uk",
)


def parsed_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return (result if result.tzinfo else result.replace(tzinfo=UTC)).astimezone(UTC)


def trusted_official_source(source_url: str, provider: str) -> bool:
    if provider.upper() in {"PLOTA", "GOVUK_APPRENTICESHIPS"}:
        return True
    hostname = (urlparse(source_url).hostname or "").lower()
    return any(
        hostname == suffix.lstrip(".") or hostname.endswith(suffix)
        for suffix in TRUSTED_SOURCE_SUFFIXES
    )


def evidence_eligibility(
    *,
    outcome_at: Any,
    available_at: Any,
    source_url: str,
    provider: str,
    case_link_confidence: str,
) -> str:
    if case_link_confidence not in CASE_LINK_CONFIDENCE:
        raise ValueError("invalid case_link_confidence")
    available = parsed_datetime(available_at)
    outcome = parsed_datetime(outcome_at)
    if available is None:
        return "EXCLUDED_NO_RELIABLE_PUBLICATION_DATE"
    if outcome is None:
        raise ValueError("benchmark outcome date is missing")
    if available > outcome:
        return "EXCLUDED_AFTER_REGISTRATION"
    if not trusted_official_source(source_url, provider):
        return "EXCLUDED_UNTRUSTWORTHY_SOURCE"
    if case_link_confidence not in {"VERIFIED_CASE_LINK", "STRONG_CASE_LINK"}:
        return "EXCLUDED_WEAK_CASE_LINK"
    return "ELIGIBLE"


def record_content_hash(record: dict[str, Any]) -> str:
    stable = {
        key: record.get(key)
        for key in (
            "vertical",
            "source_type",
            "provider",
            "external_id",
            "source_url",
            "title",
            "raw_text",
            "organisation_hint",
            "location_hint",
            "source_event_at",
            "available_at",
            "metadata",
            "provenance",
        )
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def replay_signal(record: dict[str, Any]) -> dict[str, Any]:
    """Expose only the curated point-in-time source fields to production policy code."""
    available = parsed_datetime(record.get("available_at"))
    if available is None:
        raise ValueError("historical record has no reliable availability date")
    metadata = dict(record.get("metadata") or {})
    metadata["historical_corpus"] = {
        "corpus_version": record.get("corpus_version"),
        "provider": record.get("provider"),
        "available_at": available.isoformat(),
    }
    return {
        "id": f"historical:{record['id']}",
        "schema_version": "1.0",
        "source_type": record["source_type"],
        "source_url": record["source_url"],
        "external_id": record["external_id"],
        "discovered_at": available,
        "persisted_at": record.get("retrieved_at") or available,
        "created_at": record.get("retrieved_at") or available,
        "title": record["title"],
        "raw_text": record["raw_text"],
        "location_hint": record.get("location_hint"),
        "organisation_hint": record.get("organisation_hint"),
        "metadata": metadata,
        "vertical": record["vertical"],
    }
