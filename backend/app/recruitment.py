from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("nurserysignal.recruitment")

CHILDCARE_ROLE_PATTERNS = (
    ("nursery_manager", r"\bnursery\s+(?:deputy\s+)?manager\b"),
    ("early_years_practitioner", r"\b(?:early\s+years|nursery)\s+(?:practitioner|educator)\b"),
    ("room_leader", r"\broom\s+leader\b"),
    ("preschool_practitioner", r"\bpre[- ]?school\s+practitioner\b"),
    ("early_years_teacher", r"\bearly\s+years\s+teacher\b"),
    ("nursery_nurse", r"\bnursery\s+nurse\b"),
    (
        "childcare_apprentice",
        r"\bchildcare\s+apprentice\b|\bapprentice\b.*\b(?:nursery|early\s+years)\b",
    ),
)
EXCLUDED_RECRUITMENT_PATTERNS = (
    r"\b(?:plant|tree|horticultural|garden)\s+nursery\b",
    r"\bnursery\s+stock\b",
    r"\b(?:nhs|hospital)\s+nursery\s+nurse\b",
)
EXPLICIT_CHANGE_PATTERNS = {
    "new_nursery": r"\bnew\s+(?:day\s+)?nursery\b",
    "new_setting": r"\bnew\s+(?:(?:early\s+years|childcare|nursery)\s+)?setting\b",
    "opening_soon": r"\bopening\s+soon\b",
    "expansion": r"\b(?:expan(?:sion|ding)|additional\s+provision)\b",
    "new_room": r"\bnew\s+room\b",
    "new_provision": r"\bnew\s+(?:early\s+years|childcare|nursery)\s+provision\b",
}


class RecruitmentProviderError(RuntimeError):
    """The recruitment provider returned an unusable response."""


class RecruitmentRateLimitError(RecruitmentProviderError):
    """The provider rate limited the collector."""


@dataclass(frozen=True)
class RecruitmentQuery:
    posted_since_days: int = 7
    max_records: int = 50
    page_size: int = 25
    page: int = 1

    @classmethod
    def from_event(cls, event: dict[str, Any] | None = None) -> RecruitmentQuery:
        event = event or {}
        return cls(
            posted_since_days=min(max(int(event.get("posted_since_days", 7)), 1), 31),
            max_records=min(max(int(event.get("max_records", 50)), 1), 250),
            page_size=min(max(int(event.get("page_size", 25)), 1), 100),
            page=max(int(event.get("page", 1)), 1),
        )


@dataclass(frozen=True)
class RecruitmentRecord:
    provider: str
    external_id: str
    source_url: str
    title: str
    employer_name: str | None
    workplace_name: str | None
    address: str | None
    postcode: str | None
    locality: str | None
    region: str | None
    published_at: datetime | None
    expires_at: datetime | None
    salary: str | None
    description: str
    employment_type: str | None
    raw: dict[str, Any] = field(default_factory=dict)


class RecruitmentProvider(Protocol):
    def vacancies(self, query: RecruitmentQuery) -> Iterator[RecruitmentRecord]: ...


def _text(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def normalize_gov_vacancy(record: dict[str, Any], base_url: str) -> RecruitmentRecord:
    if not isinstance(record, dict):
        raise ValueError("vacancy must be an object")
    external_id = record.get("vacancyReference") or record.get("id") or record.get("reference")
    if not external_id:
        raise ValueError("vacancy has no stable reference")
    locations = record.get("locations") or record.get("location") or []
    if isinstance(locations, dict):
        locations = [locations]
    location = locations[0] if locations and isinstance(locations[0], dict) else {}
    employer = record.get("employer") or {}
    if isinstance(employer, str):
        employer = {"name": employer}
    title = (
        _text(record.get("title") or record.get("vacancyTitle"))
        or "Untitled apprenticeship vacancy"
    )
    description = _text(record.get("description") or record.get("vacancyDescription")) or ""
    url = (
        record.get("applicationUrl")
        or record.get("vacancyUrl")
        or f"{base_url.rstrip('/')}/vacancy/{external_id}"
    )
    if not str(url).startswith(("http://", "https://")):
        raise ValueError("vacancy has no valid source URL")
    return RecruitmentRecord(
        provider="govuk-apprenticeships",
        external_id=str(external_id),
        source_url=str(url),
        title=title,
        employer_name=_text(employer.get("name") or record.get("employerName")),
        workplace_name=_text(location.get("name") or location.get("workplaceName")),
        address=_text(location.get("address") or record.get("address")),
        postcode=_text(location.get("postcode") or record.get("postcode")),
        locality=_text(location.get("town") or location.get("city") or record.get("town")),
        region=_text(location.get("county") or location.get("region")),
        published_at=_datetime(record.get("datePosted") or record.get("postedDate")),
        expires_at=_datetime(record.get("closingDate") or record.get("expiryDate")),
        salary=_text(record.get("wage") or record.get("salary")),
        description=description,
        employment_type=_text(record.get("employmentType")),
        raw=record,
    )


class GovApprenticeshipProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.apprenticeships.education.gov.uk/vacancies",
        timeout: float = 15,
        opener: Any = urlopen,
        sleep: Any = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("Find an Apprenticeship API key is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self.sleep = sleep

    def _page(self, query: RecruitmentQuery, page: int) -> dict[str, Any]:
        params = {
            "PageNumber": page,
            "PageSize": query.page_size,
            "PostedInLastNumberOfDays": query.posted_since_days,
        }
        request = Request(
            f"{self.base_url}/vacancy?{urlencode(params)}",
            headers={
                "Accept": "application/json",
                "X-Version": "2",
                "Ocp-Apim-Subscription-Key": self.api_key,
            },
        )
        for attempt in range(3):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RecruitmentProviderError("provider response is not an object")
                return payload
            except HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    self.sleep(2**attempt)
                    continue
                if exc.code == 429:
                    raise RecruitmentRateLimitError(
                        "Find an Apprenticeship API rate limit"
                    ) from exc
                raise RecruitmentProviderError(
                    f"Find an Apprenticeship API returned HTTP {exc.code}"
                ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt < 2:
                    self.sleep(2**attempt)
                    continue
                raise RecruitmentProviderError("Find an Apprenticeship API request failed") from exc
        raise RecruitmentProviderError("provider retry budget exhausted")

    def vacancies(self, query: RecruitmentQuery) -> Iterator[RecruitmentRecord]:
        fetched = 0
        page = query.page
        while fetched < query.max_records:
            payload = self._page(query, page)
            records = (
                payload.get("vacancies") or payload.get("results") or payload.get("items") or []
            )
            if not isinstance(records, list):
                raise RecruitmentProviderError("vacancy list is malformed")
            if not records:
                break
            for item in records:
                if fetched >= query.max_records:
                    break
                yield normalize_gov_vacancy(item, self.base_url)
                fetched += 1
            if len(records) < query.page_size:
                break
            page += 1


def classify_recruitment(record: RecruitmentRecord) -> dict[str, Any]:
    text = " ".join(
        value
        for value in (record.title, record.employer_name, record.workplace_name, record.description)
        if value
    ).lower()
    exclusions = [pattern for pattern in EXCLUDED_RECRUITMENT_PATTERNS if re.search(pattern, text)]
    roles = [name for name, pattern in CHILDCARE_ROLE_PATTERNS if re.search(pattern, text)]
    changes = [
        name for name, pattern in EXPLICIT_CHANGE_PATTERNS.items() if re.search(pattern, text)
    ]
    matched = bool(roles) and not exclusions
    confidence = (
        min(
            0.95,
            0.62
            + (0.12 if any("manager" in role or role == "room_leader" for role in roles) else 0)
            + (0.18 if changes else 0),
        )
        if matched
        else 0.15
    )
    return {
        "matched": matched,
        "role_categories": roles,
        "explicit_change_terms": changes,
        "exclusions": exclusions,
        "confidence": confidence,
        "likely_false_positive": bool(exclusions),
        "is_apprenticeship": any("apprentice" in role for role in roles),
    }


def recruitment_record_from_signal(raw: dict[str, Any]) -> RecruitmentRecord:
    metadata = raw.get("metadata") or {}
    provider_record = metadata.get("provider_record")
    if isinstance(provider_record, dict):
        return normalize_gov_vacancy(
            provider_record,
            str(
                metadata.get("provider_base_url")
                or "https://api.apprenticeships.education.gov.uk/vacancies"
            ),
        )
    return RecruitmentRecord(
        provider=str(metadata.get("provider") or "stored"),
        external_id=str(raw.get("external_id") or raw.get("id")),
        source_url=str(raw.get("source_url") or "https://example.invalid/stored"),
        title=str(raw.get("title") or ""),
        employer_name=raw.get("organisation_hint"),
        workplace_name=None,
        address=raw.get("location_hint"),
        postcode=metadata.get("postcode"),
        locality=metadata.get("locality"),
        region=metadata.get("region"),
        published_at=_datetime(metadata.get("published_at")),
        expires_at=_datetime(metadata.get("expires_at")),
        salary=metadata.get("salary"),
        description=str(raw.get("raw_text") or ""),
        employment_type=metadata.get("employment_type"),
        raw=provider_record if isinstance(provider_record, dict) else metadata,
    )


def recruitment_signal(record: RecruitmentRecord, decision: dict[str, Any]) -> dict[str, Any]:
    discovered = record.published_at or datetime.now(UTC)
    location = (
        ", ".join(
            value
            for value in (record.address, record.postcode, record.locality, record.region)
            if value
        )
        or None
    )
    metadata = {
        "provider": record.provider,
        "provider_vacancy_reference": record.external_id,
        "provider_record": record.raw,
        "provider_base_url": "https://api.apprenticeships.education.gov.uk/vacancies",
        "postcode": record.postcode,
        "locality": record.locality,
        "region": record.region,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "salary": record.salary,
        "employment_type": record.employment_type,
        "recruitment_classification": decision,
    }
    return {
        "schema_version": "1.0",
        "source_type": "recruitment",
        "source_url": record.source_url,
        "external_id": f"{record.provider}:{record.external_id}",
        "discovered_at": discovered.isoformat(),
        "title": record.title,
        "raw_text": record.description,
        "location_hint": location,
        "organisation_hint": record.employer_name or record.workplace_name,
        "metadata": metadata,
    }
