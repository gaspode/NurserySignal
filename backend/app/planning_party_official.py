"""Bounded, exact-URL extraction from official Idox Planning Public Access pages."""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from app.planning_parties import PLANNING_PARTY_PROVENANCE_VERSION

OFFICIAL_PARTY_SOURCE_VERSION = "official-idox-planning-party-v1"
USER_AGENT = "SignalHub/1.0 planning-party-research (contact: support@signalhub.co.uk)"


@dataclass(frozen=True)
class OfficialPartyResult:
    outcome: str
    source_url: str
    applicant_name: str | None = None
    agent_name: str | None = None
    agent_company: str | None = None
    detail: str | None = None


def idox_details_url(source_url: str) -> str | None:
    parsed = urlparse(str(source_url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if not parsed.path.lower().endswith("/applicationdetails.do"):
        return None
    query = parse_qs(parsed.query, keep_blank_values=False)
    if not query.get("keyVal"):
        return None
    query["activeTab"] = ["details"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _plain(value: str) -> str | None:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    value = " ".join(value.split()).strip(" :")
    return value or None


def _field(document: str, label: str) -> str | None:
    escaped = re.escape(label)
    patterns = (
        rf"<(?:th|td|div)[^>]*>\s*{escaped}\s*:?[\s\S]*?</(?:th|td|div)>\s*"
        rf"<(?:td|div)[^>]*>([\s\S]*?)</(?:td|div)>",
        rf"{escaped}\s*:?\s*</(?:span|strong|label)>\s*"
        rf"<(?:span|div|p)[^>]*>([\s\S]*?)</(?:span|div|p)>",
    )
    for pattern in patterns:
        match = re.search(pattern, document, flags=re.IGNORECASE)
        if match:
            return _plain(match.group(1))
    return None


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


def fetch_idox_party_page(
    source_url: str,
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: float = 12,
    retries: int = 1,
    sleeper: Callable[[float], None] = time.sleep,
) -> OfficialPartyResult:
    """Fetch one exact public Idox details page; never perform a search fallback."""
    details_url = idox_details_url(source_url)
    if not details_url:
        return OfficialPartyResult("UNSUPPORTED_AUTHORITY", source_url, detail="not an Idox URL")
    request = Request(details_url, headers={"Accept": "text/html", "User-Agent": USER_AGENT})
    attempts = min(max(int(retries), 0), 2) + 1
    for attempt in range(attempts):
        try:
            with opener(request, timeout=timeout) as response:
                body = response.read(1_000_000).decode("utf-8", errors="replace")
            break
        except HTTPError as exc:
            if exc.code == 429:
                return OfficialPartyResult("RATE_LIMITED", details_url, detail="http_429")
            if exc.code >= 500 and attempt < attempts - 1:
                sleeper(0.2 * (2**attempt))
                continue
            return OfficialPartyResult("SOURCE_UNAVAILABLE", details_url, detail=f"http_{exc.code}")
        except (TimeoutError, URLError, OSError) as exc:
            if attempt < attempts - 1:
                sleeper(0.2 * (2**attempt))
                continue
            return OfficialPartyResult(
                "SOURCE_UNAVAILABLE", details_url, detail=type(exc).__name__
            )
    applicant = _field(body, "Applicant Name")
    agent = _field(body, "Agent Name")
    agent_company = _field(body, "Agent Company Name")
    if applicant:
        outcome = (
            "APPLICANT_COMPANY_FOUND" if _company_like(applicant) else "APPLICANT_PERSON_FOUND"
        )
    elif agent or agent_company:
        outcome = "AGENT_ONLY"
    elif "Applicant Name" in body or "Agent Name" in body:
        outcome = "NO_PARTY_DATA"
    else:
        outcome = "PARSE_FAILED"
    return OfficialPartyResult(outcome, details_url, applicant, agent, agent_company)


def official_party_provenance(
    result: OfficialPartyResult, *, application_reference: str | None
) -> dict[str, Any]:
    """Translate a fetched result into the canonical explicit role schema."""
    applicant = (
        {
            "role": "APPLICANT",
            "name": result.applicant_name,
            "party_type": None,
            "company_like": _company_like(result.applicant_name),
            "company_number": None,
            "source_path": "official_idox.details.Applicant Name",
        }
        if result.applicant_name
        else None
    )
    agent = (
        {
            "role": "AGENT",
            "name": result.agent_name or result.agent_company,
            "company": result.agent_company,
            "party_type": None,
            "company_like": _company_like(result.agent_company or result.agent_name),
            "company_number": None,
            "source_path": "official_idox.details.Agent Name/Agent Company Name",
        }
        if result.agent_name or result.agent_company
        else None
    )
    return {
        "version": PLANNING_PARTY_PROVENANCE_VERSION,
        "source": "OFFICIAL_IDOX_PUBLIC_ACCESS",
        "source_version": OFFICIAL_PARTY_SOURCE_VERSION,
        "source_url": result.source_url,
        "application_reference": application_reference,
        "applicant": applicant,
        "agent": agent,
    }
