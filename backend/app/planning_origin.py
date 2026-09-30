from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.correlation import normalize_identity
from app.planning import PlanningRecord
from app.planning_families import normalize_planning_authority

_AUTHORITY_WORDS = frozenset(
    {"the", "of", "council", "county", "city", "borough", "metropolitan", "district"}
)


def authority_key(value: Any) -> str:
    normalized = normalize_planning_authority(value) or ""
    normalized = re.sub(
        r"^(?:london borough of|royal borough of|borough of|city of|council of the)\s+",
        "",
        normalized,
    )
    return " ".join(word for word in normalized.split() if word not in _AUTHORITY_WORDS)


def authorities_match(left: Any, right: Any) -> bool:
    left_normalized = normalize_planning_authority(left)
    right_normalized = normalize_planning_authority(right)
    if not left_normalized or not right_normalized:
        return False
    return left_normalized == right_normalized or (
        bool(authority_key(left)) and authority_key(left) == authority_key(right)
    )


def postcode_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def address_key(value: Any) -> str:
    return normalize_identity(value)


@dataclass(frozen=True)
class OriginCandidateResolution:
    status: str
    selected: PlanningRecord | None
    reason: str


def resolve_origin_candidate(
    candidates: tuple[PlanningRecord, ...],
    *,
    authority: str,
    postcode: str | None,
    address: str | None,
    truncated: bool,
) -> OriginCandidateResolution:
    """Resolve exact-reference candidates conservatively from local site context."""
    if not candidates:
        return OriginCandidateResolution("NOT_FOUND", None, "provider_returned_zero_exact_matches")
    if truncated:
        return OriginCandidateResolution("AMBIGUOUS", None, "provider_candidate_set_truncated")
    if len(candidates) == 1:
        return OriginCandidateResolution("FOUND", candidates[0], "single_exact_reference_match")

    authority_matches = tuple(
        candidate for candidate in candidates if authorities_match(authority, candidate.council)
    )
    if len(authority_matches) == 1:
        return OriginCandidateResolution(
            "FOUND", authority_matches[0], "unique_normalized_authority_match"
        )
    pool = authority_matches or candidates

    wanted_postcode = postcode_key(postcode)
    if wanted_postcode:
        postcode_matches = tuple(
            candidate for candidate in pool if postcode_key(candidate.postcode) == wanted_postcode
        )
        if len(postcode_matches) == 1:
            return OriginCandidateResolution("FOUND", postcode_matches[0], "unique_postcode_match")
        if postcode_matches:
            pool = postcode_matches

    wanted_address = address_key(address)
    if wanted_address:
        address_matches = tuple(
            candidate for candidate in pool if address_key(candidate.address) == wanted_address
        )
        if len(address_matches) == 1:
            return OriginCandidateResolution("FOUND", address_matches[0], "unique_address_match")

    reason = "multiple_context_matches" if authority_matches else "no_context_match"
    return OriginCandidateResolution("AMBIGUOUS", None, reason)


def candidate_summary(candidate: PlanningRecord) -> dict[str, Any]:
    reference = candidate.raw.get("reference") or candidate.raw.get("application_reference")
    return {
        "provider_id": candidate.application_id,
        "reference": reference,
        "authority": candidate.council,
        "address": candidate.address,
        "postcode": candidate.postcode,
        "application_date": candidate.application_date.isoformat()
        if candidate.application_date
        else None,
        "proposal": candidate.description[:300],
        "source_url": candidate.application_url,
    }
