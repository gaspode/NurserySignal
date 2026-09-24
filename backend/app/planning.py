from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.classification import classify_signal_text, combined_text

logger = logging.getLogger("nurserysignal.planning")

POSITIVE_TERMS = (
    "nursery",
    "day nursery",
    "children's nursery",
    "childrens nursery",
    "childcare",
    "child care",
    "preschool",
    "pre-school",
    "early years",
    "crèche",
    "creche",
    "montessori",
)
EXCLUSION_PATTERNS = (
    r"\bcommunity\s+garden\s+nurser(?:y|ies)\b",
    r"\bgarden\s+nurser(?:y|ies)\b",
    r"\bplant\s+nurser(?:y|ies)\b",
    r"\btree\s+nurser(?:y|ies)\b",
    r"\bhorticultural\s+nurser(?:y|ies)\b",
    r"\bnursery\s+stock\b",
    r"\bplant(?:s|ing)?\b",
    r"\bgardening\b",
    r"\bhorticultur(?:e|al)\b",
    r"\brhs\b",
    r"\bgarden\s+centr(?:e|er)\b",
    r"\bgrowing\s+plants?\b",
    r"\bpropagation\b",
    r"\bseedlings?\b",
    r"\bsaplings?\b",
    r"\bforest\s+nurser(?:y|ies)\b",
    r"\bprimary\s+and\s+nurser(?:y|ies)\b",
    r"\b(?:primary|secondary|infant|junior)\s+school\s+and\s+nurser(?:y|ies)\b",
    r"\b(?:primary|secondary|infant|junior)\s+school\s+nurser(?:y|ies)\b",
    r"\bnursery\s+(?:bedroom|room)\b",
    r"\bbedroom\s+(?:nursery|for\s+a\s+nursery)\b",
    r"\bschool\s+nursery\s+class(?:es)?\b",
    r"\bnursery\s+class(?:es)?\b",
    r"\bnursery\s+school\b",
    r"\bnon[- ]material\s+amendment\b",
    r"\bdetails\s+pursuant\s+to\s+condition\b",
    r"\bdischarge\s+of\s+(?:a\s+)?condition(?:s)?\b",
)


class PlanningProviderError(RuntimeError):
    """The provider could not return a usable page."""


class PlanningRateLimitError(PlanningProviderError):
    """The provider rate limited the collector after its retry budget."""


@dataclass(frozen=True)
class PlanningQuery:
    from_date: date
    to_date: date
    search_term: str = "nursery"
    council: str | None = None
    max_records: int = 100
    page_size: int = 50

    @classmethod
    def from_event(cls, event: dict[str, Any] | None = None) -> PlanningQuery:
        event = event or {}
        now = datetime.now(UTC).date()
        from_date = date.fromisoformat(str(event.get("from_date", now - timedelta(days=2))))
        to_date = date.fromisoformat(str(event.get("to_date", now)))
        if to_date < from_date:
            raise ValueError("to_date must not be before from_date")
        max_records = min(max(int(event.get("max_records", 100)), 1), 500)
        page_size = min(max(int(event.get("page_size", 50)), 1), 250)
        return cls(
            from_date=from_date,
            to_date=to_date,
            search_term=str(event.get("search_term", "nursery")),
            council=str(event["council"]) if event.get("council") else None,
            max_records=max_records,
            page_size=page_size,
        )


@dataclass(frozen=True)
class PlanningRecord:
    provider: str
    application_id: str
    application_url: str
    description: str
    address: str | None
    postcode: str | None
    latitude: float | None
    longitude: float | None
    application_date: date | None
    status: str | None
    decision: str | None
    decision_date: date | None
    council: str | None
    applicant: str | None
    agent: str | None
    raw: dict[str, Any] = field(default_factory=dict)


class PlanningProvider(Protocol):
    def applications(self, query: PlanningQuery) -> Iterator[PlanningRecord]: ...


def planning_record_from_signal(raw: dict[str, Any]) -> PlanningRecord:
    """Reconstruct a stored planning record without calling the provider.

    Collector records retain the complete provider response in signal metadata.
    Reprocessing uses that durable copy and deliberately does not perform a new
    provider request.
    """
    metadata = raw.get("metadata") or {}
    provider_record = metadata.get("provider_record")
    if isinstance(provider_record, dict):
        return normalize_plota_record(
            provider_record,
            str(metadata.get("provider_base_url") or "https://api.plota.co.uk/v1"),
        )
    return PlanningRecord(
        provider=str(metadata.get("provider") or "stored"),
        application_id=str(
            metadata.get("provider_application_id") or raw.get("external_id") or raw["id"]
        ),
        application_url=str(raw.get("source_url") or "https://example.invalid/stored"),
        description=str(raw.get("raw_text") or raw.get("title") or ""),
        address=raw.get("location_hint"),
        postcode=metadata.get("postcode"),
        latitude=metadata.get("latitude"),
        longitude=metadata.get("longitude"),
        application_date=_date(metadata.get("application_date")),
        status=metadata.get("planning_status"),
        decision=metadata.get("decision"),
        decision_date=_date(metadata.get("decision_date")),
        council=metadata.get("council"),
        applicant=metadata.get("applicant") or raw.get("organisation_hint"),
        agent=metadata.get("agent"),
        raw=provider_record if isinstance(provider_record, dict) else metadata,
    )


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_plota_record(record: dict[str, Any], base_url: str) -> PlanningRecord:
    if not isinstance(record, dict):
        raise ValueError("provider application must be an object")
    application_id = record.get("id") or record.get("reference")
    if not application_id:
        raise ValueError("provider application has no stable id")
    links = record.get("links") or {}
    authority = record.get("authority") or {}
    location = record.get("location") or {}
    decision = record.get("decision") or {}
    application_url = links.get("council") or links.get("plota")
    if not application_url:
        application_url = f"{base_url.rstrip('/')}/application/{application_id}"
    if not str(application_url).startswith(("http://", "https://")):
        raise ValueError("provider application has no valid source URL")
    return PlanningRecord(
        provider="plota",
        application_id=str(application_id),
        application_url=str(application_url),
        description=str(record.get("description") or ""),
        address=str(record["address"]) if record.get("address") else None,
        postcode=str(record["postcode"]) if record.get("postcode") else None,
        latitude=_float(location.get("lat")),
        longitude=_float(location.get("lng")),
        application_date=_date(record.get("date_received")),
        status=str(record["status"]) if record.get("status") else None,
        decision=str(decision["outcome"]) if decision.get("outcome") else None,
        decision_date=_date(record.get("date_decided") or decision.get("issued_date")),
        council=str(authority.get("name")) if authority.get("name") else None,
        applicant=str(record["applicant"]) if record.get("applicant") else None,
        agent=str(record["agent"]) if record.get("agent") else None,
        raw=record,
    )


class PlotaProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.plota.co.uk/v1",
        timeout: float = 15,
        opener: Any = urlopen,
        sleep: Any = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("Plota API key is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self.sleep = sleep

    def _page(self, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/applications?{urlencode(params)}"
        request = Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        for attempt in range(3):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read())
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise PlanningProviderError("Plota response has an invalid shape")
                return payload
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt == 2:
                        raise PlanningRateLimitError("Plota rate limit exceeded") from exc
                    retry_after = int(exc.headers.get("Retry-After", "2"))
                    self.sleep(min(max(retry_after, 1), 30))
                    continue
                if exc.code >= 500 and attempt < 2:
                    self.sleep(2**attempt)
                    continue
                raise PlanningProviderError(f"Plota HTTP error {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise PlanningProviderError("Plota request failed") from exc
                self.sleep(2**attempt)
            except (json.JSONDecodeError, ValueError) as exc:
                raise PlanningProviderError("Plota returned invalid JSON") from exc
        raise PlanningProviderError("Plota request failed")

    def applications(self, query: PlanningQuery) -> Iterator[PlanningRecord]:
        cursor: str | None = None
        yielded = 0
        while yielded < query.max_records:
            params: dict[str, Any] = {
                "nation": "england",
                "date_from": query.from_date.isoformat(),
                "date_to": query.to_date.isoformat(),
                "q": query.search_term,
                "limit": min(query.page_size, query.max_records - yielded),
                "include_contact": "false",
            }
            if query.council:
                params["council"] = query.council
            if cursor:
                params["cursor"] = cursor
            payload = self._page(params)
            rows = payload["data"]
            if not rows:
                break
            for row in rows:
                yield normalize_plota_record(row, self.base_url)
                yielded += 1
                if yielded >= query.max_records:
                    break
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break


@dataclass(frozen=True)
class CandidateDecision:
    matched: bool
    positive_terms: tuple[str, ...]
    exclusions: tuple[str, ...]
    childcare_terms: tuple[str, ...] = ()
    horticultural_terms: tuple[str, ...] = ()
    likely_false_positive: bool = False


def candidate_decision(record: PlanningRecord) -> CandidateDecision:
    classification = classify_signal_text(
        (
            record.description,
            record.address,
            record.status,
            record.decision,
            record.council,
            record.applicant,
            record.agent,
            record.raw,
        )
    )
    text = classification.text
    # A generic "nursery" in an address or applicant name is not enough to
    # make a planning application a candidate.  Use the proposal/status and
    # structured planning fields for positive matching, while retaining the
    # complete record for exclusions and provenance analysis.
    proposal_text = combined_text(
        (
            record.description,
            record.status,
            record.decision,
            (record.raw or {}).get("description"),
            (record.raw or {}).get("category"),
            (record.raw or {}).get("categories"),
            (record.raw or {}).get("planning_route"),
        )
    )
    positive = tuple(term for term in POSITIVE_TERMS if term in proposal_text)
    exclusions = tuple(
        pattern for pattern in EXCLUSION_PATTERNS if re.search(pattern, text, re.IGNORECASE)
    )
    strong_childcare_context = bool(classification.childcare_terms)
    matched = bool(positive) and not classification.likely_false_positive and (
        not exclusions or strong_childcare_context
    )
    return CandidateDecision(
        matched,
        positive,
        exclusions,
        classification.childcare_terms,
        classification.horticultural_terms,
        classification.likely_false_positive,
    )


def planning_signal(record: PlanningRecord, decision: CandidateDecision) -> dict[str, Any]:
    status = record.status or record.decision or "pending"
    title = record.description or f"Planning application {record.application_id}"
    raw_text = "\n".join(
        value
        for value in (
            title,
            f"Status: {status}" if status else "",
            f"Council: {record.council}" if record.council else "",
        )
        if value
    )
    metadata = {
        "provider": record.provider,
        "provider_application_id": record.application_id,
        "council": record.council,
        "postcode": record.postcode,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "application_date": (
            record.application_date.isoformat() if record.application_date else None
        ),
        "planning_status": record.status,
        "decision": record.decision,
        "decision_date": record.decision_date.isoformat() if record.decision_date else None,
        "applicant": record.applicant,
        "agent": record.agent,
        "candidate_positive_terms": list(decision.positive_terms),
        "candidate_exclusions": list(decision.exclusions),
        "candidate_childcare_terms": list(decision.childcare_terms),
        "candidate_horticultural_terms": list(decision.horticultural_terms),
        "provider_record": record.raw,
    }
    return {
        "schema_version": "1.0",
        "source_type": "planning",
        "source_url": record.application_url,
        "external_id": f"plota:{record.application_id}",
        "discovered_at": datetime.now(UTC).isoformat(),
        "title": title[:500],
        "raw_text": raw_text[:100_000],
        "location_hint": record.address,
        "organisation_hint": record.applicant or record.agent,
        "metadata": metadata,
    }
