from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIND_A_TENDER = "find_a_tender"
CONTRACTS_FINDER = "contracts_finder"

PLATFORM_URLS = {
    FIND_A_TENDER: "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
    CONTRACTS_FINDER: (
        "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    ),
}

# A small, versioned evaluation corpus found through official notice search.
# It prevents rare high-value cases being lost in the broad feed sample and is
# not used by classification or production matching.
EVALUATION_RECORD_URLS = {
    "camden-three-new-homes": (
        FIND_A_TENDER,
        f"{PLATFORM_URLS[FIND_A_TENDER]}/ocds-h6vhtk-0649ec",
    ),
    "hackney-two-new-homes": (
        FIND_A_TENDER,
        f"{PLATFORM_URLS[FIND_A_TENDER]}/ocds-h6vhtk-0614f1",
    ),
    "ypo-childrens-homes-framework": (
        FIND_A_TENDER,
        f"{PLATFORM_URLS[FIND_A_TENDER]}/ocds-h6vhtk-05dbf4",
    ),
    "hampshire-new-homes": (
        FIND_A_TENDER,
        f"{PLATFORM_URLS[FIND_A_TENDER]}/025474-2025",
    ),
    "milton-keynes-home-award": (
        CONTRACTS_FINDER,
        "https://www.contractsfinder.service.gov.uk/Published/Notice/releases/"
        "5dec1ece-fd8d-4ff2-804a-352ab8dccccf.json",
    ),
}

CLASSIFICATIONS = {
    "NEW_HOME_COMMISSIONING",
    "NEW_CAPACITY_MARKET_ENGAGEMENT",
    "OPERATOR_PROCUREMENT",
    "CONTRACT_AWARD",
    "ROUTINE_PLACEMENT_FRAMEWORK",
    "EXISTING_SERVICE_REPROCUREMENT",
    "UNCERTAIN",
    "IRRELEVANT",
}

CHILDRENS_HOME_CONTEXT = (
    r"\bchildren(?:'s|s)?\s+(?:residential\s+)?(?:care\s+)?homes?\b",
    r"\bresidential\s+(?:care\s+)?homes?\s+for\s+(?:children|young people)\b",
    r"\bresidential\s+childcare\b",
    r"\blooked[- ]after children\b.{0,80}\bresidential\b",
)
NEW_PROVISION = (
    r"\b(?:commission|establish|develop|create|build|acquire|open)(?:ing|ed|s)?\b.{0,90}"
    r"\b(?:new|additional)\b.{0,45}\bchildren(?:'s|s)?\s+(?:care\s+)?homes?\b",
    r"\b(?:new|additional)\s+(?:residential\s+)?children(?:'s|s)?\s+homes?\b",
    r"\bnewly\s+created\s+(?:residential\s+)?children(?:'s|s)?\s+homes?\b",
    r"\bnew\s+(?:residential\s+)?(?:care\s+)?capacity\b.{0,80}\bchildren\b",
)
OPERATOR_SEARCH = (
    r"\b(?:seek|seeking|appoint|procure|select)(?:ing|s|ed)?\b.{0,100}"
    r"\b(?:provider|operator)\b.{0,100}\b(?:new|council[- ]owned)\b.{0,50}\bhome",
    r"\bprovider\s+(?:is\s+)?required\s+to\s+(?:operate|run|open)\b.{0,80}\bhome",
    r"\bcommission\b.{0,50}\bprovider\b.{0,60}\b(?:operate|run)\b.{0,60}"
    r"\bnew\b.{0,30}\bchildren(?:'s|s)?\s+home",
)
MARKET_ENGAGEMENT = (
    r"\b(?:market engagement|prior information|pipeline notice|soft market testing)\b",
)
ROUTINE_FRAMEWORK = (
    r"\b(?:placement|spot purchase|spot-purchase)\b.{0,70}\b(?:framework|agreement|contract)",
    r"\bframework\b.{0,100}\b(?:placements|residential care placements)\b",
    r"\bchildren(?:'s|s)?\s+homes?\s+framework\b",
)
REPROCUREMENT = (
    r"\b(?:reprocurement|re-procurement|renewal|replacement contract|existing service)\b",
)
EXCLUSIONS = (
    r"\b(?:adult|elderly|older people|nursing home|domiciliary care)\b",
    r"\bsupported living\b",
    r"\bfoster(?:ing| care)\b",
)


@dataclass(frozen=True)
class ProcurementQuery:
    published_since_days: int = 548
    max_records_per_source: int = 300
    page_size: int = 100

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ProcurementQuery:
        days = min(max(int(event.get("published_since_days", 548)), 1), 548)
        maximum = min(max(int(event.get("max_records_per_source", 300)), 1), 300)
        page_size = min(max(int(event.get("page_size", 100)), 1), 100)
        return cls(days, maximum, page_size)


def _json_get(url: str, *, timeout: int = 25) -> dict[str, Any]:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "SignalHub/1.0"},
        method="GET",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 official fixed hosts
                payload = json.loads(response.read())
            break
        except HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                detail = exc.read(500).decode("utf-8", errors="replace").replace("\n", " ")
                raise RuntimeError(f"procurement provider HTTP {exc.code}: {detail[:300]}") from exc
            retry_after = min(max(int(exc.headers.get("Retry-After") or "2"), 1), 5)
            time.sleep(retry_after)
    if not isinstance(payload, dict):
        raise ValueError("procurement provider returned a non-object response")
    return payload


def iter_releases(
    platform: str,
    query: ProcurementQuery,
    *,
    now: datetime | None = None,
    fetch: Callable[[str], dict[str, Any]] = _json_get,
) -> Iterator[dict[str, Any]]:
    if platform not in PLATFORM_URLS:
        raise ValueError("unsupported procurement platform")
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = end - timedelta(days=query.published_since_days)
    # The official services enforce a shared 12-request window. Three stratified
    # windows per platform plus the targeted records stay within that boundary.
    windows = min(3, max(1, ceil(query.published_since_days / 31)))
    window_quota = max(1, ceil(query.max_records_per_source / windows))
    window_days = query.published_since_days / windows
    emitted = 0
    window_start = start
    while window_start < end and emitted < query.max_records_per_source:
        window_end = min(window_start + timedelta(days=window_days), end)
        params = {
            "limit": min(query.page_size, window_quota),
            ("updatedFrom" if platform == FIND_A_TENDER else "publishedFrom"): (
                window_start.isoformat().replace("+00:00", "Z")
            ),
            ("updatedTo" if platform == FIND_A_TENDER else "publishedTo"): (
                window_end.isoformat().replace("+00:00", "Z")
            ),
        }
        url = f"{PLATFORM_URLS[platform]}?{urlencode(params)}"
        seen_urls: set[str] = set()
        window_emitted = 0
        while url and window_emitted < window_quota and emitted < query.max_records_per_source:
            if url in seen_urls:
                raise ValueError("procurement provider returned a pagination loop")
            seen_urls.add(url)
            package = fetch(url)
            for release in package.get("releases") or []:
                if not isinstance(release, dict):
                    continue
                yield release
                emitted += 1
                window_emitted += 1
                if (
                    window_emitted >= window_quota
                    or emitted >= query.max_records_per_source
                ):
                    break
            next_url = (package.get("links") or {}).get("next")
            url = str(next_url) if next_url else ""
            if url and fetch is _json_get:
                time.sleep(0.2)
        window_start = window_end


def iter_evaluation_records(
    *, fetch: Callable[[str], dict[str, Any]] = _json_get
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for research_key, (platform, url) in EVALUATION_RECORD_URLS.items():
        package = fetch(url)
        for release in package.get("releases") or []:
            if isinstance(release, dict):
                yield research_key, platform, release


def _strip_contacts(release: dict[str, Any]) -> dict[str, Any]:
    """Retain official organisation evidence while dropping unnecessary people/contact data."""
    cleaned = copy.deepcopy(release)
    for party in cleaned.get("parties") or []:
        if isinstance(party, dict):
            party.pop("contactPoint", None)
    return cleaned


def _text(release: dict[str, Any]) -> str:
    tender = release.get("tender") or {}
    awards = release.get("awards") or []
    values = [tender.get("title"), tender.get("description")]
    for award in awards:
        if isinstance(award, dict):
            values.extend((award.get("title"), award.get("description")))
    return " ".join(str(value or "") for value in values).lower().replace("’", "'")


def classify_procurement(release: dict[str, Any]) -> dict[str, Any]:
    text = _text(release)
    child_home = any(re.search(pattern, text) for pattern in CHILDRENS_HOME_CONTEXT)
    exclusions = [pattern for pattern in EXCLUSIONS if re.search(pattern, text)]
    new_terms = [pattern for pattern in NEW_PROVISION if re.search(pattern, text)]
    operator_terms = [pattern for pattern in OPERATOR_SEARCH if re.search(pattern, text)]
    future_operator_context = bool(
        re.search(
            r"\bprovider\b.{0,100}\b(?:operate|run)\b.{0,120}"
            r"\b(?:residential\s+)?children(?:'s|s)?\s+home",
            text,
        )
        and re.search(
            r"\b(?:council[- ]owned|council's property|will be ofsted[- ]registered)\b",
            text,
        )
    )
    if future_operator_context:
        operator_terms.append("future_operator_for_council_home")
    engagement = any(re.search(pattern, text) for pattern in MARKET_ENGAGEMENT)
    routine = any(re.search(pattern, text) for pattern in ROUTINE_FRAMEWORK)
    explicit_physical_new = bool(
        re.search(
            r"\b(?:new|additional)\s+(?:residential\s+)?"
            r"children(?:'s|s)?\s+homes?\b",
            text,
        )
    )
    reprocurement = any(re.search(pattern, text) for pattern in REPROCUREMENT)
    tags = {str(tag).lower() for tag in release.get("tag") or []}
    awards = [award for award in release.get("awards") or [] if isinstance(award, dict)]

    if exclusions or not child_home:
        category, confidence = "IRRELEVANT", 0.12 if not child_home else 0.2
        reason = "No specific children's-home commissioning context"
    elif routine and not (explicit_physical_new or operator_terms):
        category, confidence = "ROUTINE_PLACEMENT_FRAMEWORK", 0.9
        reason = "Children's residential placements/framework without new-home intent"
    elif reprocurement and not new_terms:
        category, confidence = "EXISTING_SERVICE_REPROCUREMENT", 0.86
        reason = "Existing-service renewal or reprocurement"
    elif awards and (new_terms or operator_terms):
        category, confidence = "CONTRACT_AWARD", 0.94
        reason = "Award-stage evidence for explicit new children's-home provision"
    elif operator_terms:
        category, confidence = "OPERATOR_PROCUREMENT", 0.92
        reason = "Authority is seeking an operator/provider for new home provision"
    elif new_terms and (engagement or "planning" in tags):
        category, confidence = "NEW_CAPACITY_MARKET_ENGAGEMENT", 0.88
        reason = "Pre-procurement engagement for explicit new residential capacity"
    elif new_terms:
        category, confidence = "NEW_HOME_COMMISSIONING", 0.92
        reason = "Notice explicitly commissions new children's-home provision"
    else:
        category, confidence = "UNCERTAIN", 0.5
        reason = "Children's-home procurement is present but new physical provision is unclear"
    return {
        "category": category,
        "confidence": confidence,
        "reason": reason,
        "strong_candidate": category
        in {
            "NEW_HOME_COMMISSIONING",
            "NEW_CAPACITY_MARKET_ENGAGEMENT",
            "OPERATOR_PROCUREMENT",
            "CONTRACT_AWARD",
        },
        "matched_change_terms": new_terms + operator_terms,
        "exclusions": exclusions,
    }


def same_procurement_process(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """OCDS OCID is authoritative across planning, tender and award releases."""
    left_ocid = str(left.get("ocid") or "").strip()
    right_ocid = str(right.get("ocid") or "").strip()
    return bool(left_ocid and left_ocid == right_ocid)


def _party(release: dict[str, Any], role: str) -> dict[str, Any]:
    for party in release.get("parties") or []:
        if isinstance(party, dict) and role in (party.get("roles") or []):
            return party
    return {}


def procurement_signal(
    platform: str, release: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    release = _strip_contacts(release)
    decision = classify_procurement(release)
    tender = release.get("tender") or {}
    buyer = release.get("buyer") or _party(release, "buyer")
    buyer_party = _party(release, "buyer")
    buyer_address = buyer_party.get("address") or {}
    awards = [award for award in release.get("awards") or [] if isinstance(award, dict)]
    suppliers: list[dict[str, Any]] = []
    for award in awards:
        for supplier in award.get("suppliers") or []:
            if isinstance(supplier, dict):
                suppliers.append({"id": supplier.get("id"), "name": supplier.get("name")})
    source_url = next(
        (
            document.get("url")
            for document in tender.get("documents") or []
            if isinstance(document, dict) and document.get("url")
        ),
        f"{PLATFORM_URLS[platform]}?ocid={release.get('ocid', '')}",
    )
    published_at = tender.get("datePublished") or release.get("date")
    if not published_at:
        raise ValueError("procurement release has no publication date")
    title = str(tender.get("title") or (awards[0].get("title") if awards else "Procurement notice"))
    raw_text = str(
        tender.get("description")
        or " ".join(str(award.get("description") or "") for award in awards)
    )
    location = " · ".join(
        str(value)
        for value in (buyer_address.get("locality"), buyer_address.get("region"))
        if value
    ) or None
    metadata = {
        "procurement_platform": platform,
        "ocid": release.get("ocid"),
        "release_id": release.get("id"),
        "notice_stage": (release.get("tag") or [None])[0],
        "publication_date": published_at,
        "modified_date": release.get("date"),
        "buyer_name": buyer.get("name"),
        "buyer_id": buyer.get("id"),
        "buyer_locality": buyer_address.get("locality"),
        "buyer_region": buyer_address.get("region"),
        "buyer_postcode": buyer_address.get("postalCode"),
        "procurement_category": decision["category"],
        "procurement_confidence": decision["confidence"],
        "procurement_reason": decision["reason"],
        "strong_candidate": decision["strong_candidate"],
        "contract_start_date": (tender.get("contractPeriod") or {}).get("startDate"),
        "contract_end_date": (tender.get("contractPeriod") or {}).get("endDate"),
        "estimated_value": (tender.get("value") or {}).get("amount"),
        "currency": (tender.get("value") or {}).get("currency"),
        "suppliers": suppliers,
        "location_sensitivity": "STANDARD",
        "evaluation_mode": "SHADOW_ONLY",
    }
    signal = {
        "schema_version": "1.0",
        "vertical": "CHILDRENS_HOME",
        "source_type": "procurement",
        "source_url": source_url,
        "external_id": f"{platform}:{release.get('id') or release.get('ocid')}",
        "discovered_at": published_at,
        "title": title,
        "raw_text": raw_text or title,
        "location_hint": location,
        "organisation_hint": str(buyer.get("name") or "") or None,
        "metadata": metadata,
    }
    return signal, release
