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
PROVIDER_USER_AGENT = "NurserySignal/1.0"
MAX_PROVIDER_ERROR_BODY = 512

ROLE_PATTERNS = (
    ("nursery_manager", r"\bnursery\s+(?:deputy\s+)?manager\b"),
    ("early_years_educator", r"\bearly\s+years\s+educator\b"),
    ("early_years_practitioner", r"\b(?:early\s+years|nursery)\s+practitioner\b"),
    ("room_leader", r"\broom\s+leader\b"),
    ("preschool_practitioner", r"\bpre[- ]?school\s+practitioner\b"),
    ("early_years_teacher", r"\bearly\s+years\s+teacher\b"),
    ("nursery_nurse", r"\bnursery\s+nurse\b"),
    (
        "childcare_apprentice",
        r"\b(?:childcare|early\s+years|nursery)\s+(?:apprentice|apprenticeship)\b"
        r"|\bapprentice\s+(?:nursery|early\s+years|childcare)\b",
    ),
    ("teaching_assistant", r"\bteaching\s+assistant\b"),
)
OTHER_EDUCATION_ROLE_PATTERN = (
    r"\b(?:teacher|classroom|learning\s+support|school)\b"
    r"\s+(?:assistant|role|teacher)?\b|\b(?:teacher|education)\b"
)
SETTING_PATTERNS = (
    ("nursery_school", r"\bnursery\s+school\b"),
    (
        "school_with_nursery",
        r"\b(?:academy|primary|infant|junior|secondary|school)\b.*\bnursery\b"
        r"|\bnursery\b.*\b(?:academy|primary|infant|junior|secondary|school)\b",
    ),
    ("preschool", r"\bpre[- ]?school\b|\bpre[- ]?school\s+setting\b"),
    ("early_years_setting", r"\bearly\s+years\b|\beyfs\b"),
    ("childcare_setting", r"\bchildcare\b|\bday\s+nursery\b|\bcreche\b|\bcrèche\b"),
    ("nursery", r"\bnursery\b"),
    ("generic_school", r"\b(?:academy|primary|infant|junior|secondary)\s+school\b|\bschool\b"),
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

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RecruitmentRateLimitError(RecruitmentProviderError):
    """The provider rate limited the collector."""


def _safe_error_body(raw: bytes, api_key: str) -> str:
    """Return a bounded provider error body with credentials removed."""
    body = raw.decode("utf-8", errors="replace")[:MAX_PROVIDER_ERROR_BODY]
    body = body.replace(api_key, "[REDACTED]")
    body = re.sub(
        r"(?i)(ocp-apim-subscription-key|api[_-]?key)(\s*[=:]\s*)[^,;\s}\"]+",
        r"\1\2[REDACTED]",
        body,
    )
    return body


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
    latitude: float | None = None
    longitude: float | None = None


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


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _first_location(record: dict[str, Any]) -> dict[str, Any]:
    locations = record.get("addresses") or record.get("locations") or record.get("location") or []
    if isinstance(locations, dict):
        return locations
    if isinstance(locations, list):
        return next((item for item in locations if isinstance(item, dict)), {})
    return {}


def _address_text(location: dict[str, Any], record: dict[str, Any]) -> str | None:
    parts = [
        location.get(key)
        for key in ("addressLine1", "addressLine2", "addressLine3", "addressLine4")
    ]
    if not any(parts):
        parts = [location.get("address") or record.get("address")]
    values = [_text(value) for value in parts]
    return ", ".join(value for value in values if value) or None
def normalize_gov_vacancy(record: dict[str, Any], base_url: str) -> RecruitmentRecord:
    if not isinstance(record, dict):
        raise ValueError("vacancy must be an object")
    external_id = record.get("vacancyReference") or record.get("id") or record.get("reference")
    if not external_id:
        raise ValueError("vacancy has no stable reference")
    location = _first_location(record)
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
        address=_address_text(location, record),
        postcode=_text(location.get("postcode") or record.get("postcode")),
        locality=_text(
            location.get("town")
            or location.get("city")
            or location.get("locality")
            or record.get("town")
        ),
        region=_text(location.get("county") or location.get("region") or record.get("region")),
        published_at=_datetime(record.get("datePosted") or record.get("postedDate")),
        expires_at=_datetime(record.get("closingDate") or record.get("expiryDate")),
        salary=_text(record.get("wage") or record.get("salary")),
        description=description,
        employment_type=_text(record.get("employmentType")),
        raw=record,
        latitude=_number(location.get("latitude") or record.get("latitude")),
        longitude=_number(location.get("longitude") or record.get("longitude")),
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
                "User-Agent": PROVIDER_USER_AGENT,
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
                        f"Find an Apprenticeship API rate limit: "
                        f"{_safe_error_body(exc.read(), self.api_key)}",
                        status_code=exc.code,
                    ) from exc
                detail = _safe_error_body(exc.read(), self.api_key)
                logger.warning(
                    "recruitment_provider_http_error status=%d body=%s",
                    exc.code,
                    detail or "<empty>",
                )
                raise RecruitmentProviderError(
                    f"Find an Apprenticeship API returned HTTP {exc.code}"
                    + (f": {detail}" if detail else ""),
                    status_code=exc.code,
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
    title = record.title.lower()
    course = record.raw.get("course") if isinstance(record.raw.get("course"), dict) else {}
    course_text = " ".join(
        str(value).lower() for value in (course.get("title"), course.get("route")) if value
    )
    role_text = " ".join(value for value in (title, course_text) if value)
    setting_text = " ".join(
        value.lower()
        for value in (record.employer_name, record.workplace_name, record.description, title)
        if value
    )
    full_text = " ".join(value for value in (role_text, setting_text) if value)
    exclusions = [
        pattern for pattern in EXCLUDED_RECRUITMENT_PATTERNS if re.search(pattern, full_text)
    ]
    title_roles = [name for name, pattern in ROLE_PATTERNS if re.search(pattern, title)]
    course_roles = [name for name, pattern in ROLE_PATTERNS if re.search(pattern, course_text)]
    # Prefer the advertised title: a course route such as "early years
    # educator" must not relabel an explicit "childcare apprenticeship" role.
    roles = title_roles or course_roles
    if not roles and re.search(OTHER_EDUCATION_ROLE_PATTERN, role_text):
        roles = ["other_education_role"]
    setting_categories = [
        name for name, pattern in SETTING_PATTERNS if re.search(pattern, setting_text)
    ]
    setting_categories = list(dict.fromkeys(setting_categories))
    matched_setting_terms = [
        category for category in setting_categories if category != "generic_school"
    ]
    changes = [
        name for name, pattern in EXPLICIT_CHANGE_PATTERNS.items() if re.search(pattern, full_text)
    ]
    childcare_roles = {
        "nursery_manager",
        "early_years_educator",
        "early_years_practitioner",
        "room_leader",
        "preschool_practitioner",
        "early_years_teacher",
        "nursery_nurse",
        "childcare_apprentice",
    }
    has_setting = bool(matched_setting_terms)
    direct_childcare_role = bool(set(roles) & childcare_roles)
    teaching_assistant_at_nursery = "teaching_assistant" in roles and has_setting
    matched = not exclusions and (direct_childcare_role or teaching_assistant_at_nursery)
    ambiguity_flags: list[str] = []
    if "teaching_assistant" in roles and not has_setting:
        ambiguity_flags.append("generic_teaching_assistant")
    if roles == ["other_education_role"]:
        ambiguity_flags.append("generic_education_role")
    if not setting_categories:
        ambiguity_flags.append("setting_unknown")
    commercial_change_evidence = "STRONG" if changes else "NONE"
    if not changes and any(role in roles for role in ("nursery_manager", "room_leader")):
        commercial_change_evidence = "WEAK"
    if not matched:
        relevance = "IRRELEVANT"
    elif changes:
        relevance = "RELEVANT_CHANGE"
    elif ambiguity_flags and "setting_unknown" in ambiguity_flags:
        relevance = "UNCERTAIN"
    else:
        relevance = "RELEVANT_ROUTINE"
    confidence = 0.15
    if matched:
        confidence = 0.58
        if direct_childcare_role:
            confidence += 0.16
        if any(role in roles for role in ("early_years_educator", "early_years_practitioner")):
            confidence += 0.08
        if "nursery_manager" in roles or "room_leader" in roles:
            confidence += 0.08
        if teaching_assistant_at_nursery:
            confidence -= 0.10
        if changes:
            confidence += 0.12
        if relevance == "UNCERTAIN":
            confidence -= 0.12
        confidence = min(0.95, max(0.15, confidence))
    return {
        "matched": matched,
        "role_categories": roles,
        "role_category": roles[0] if roles else "other",
        "setting_categories": setting_categories,
        "setting_category": setting_categories[0] if setting_categories else "unknown",
        "relevance": relevance,
        "commercial_change_evidence": commercial_change_evidence,
        "explicit_change_terms": changes,
        "exclusions": exclusions,
        "confidence": confidence,
        "likely_false_positive": bool(exclusions),
        "is_apprenticeship": any("apprentice" in role for role in roles),
        "matched_role_terms": roles,
        "matched_setting_terms": matched_setting_terms,
        "ambiguity_flags": ambiguity_flags,
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
        "latitude": record.latitude,
        "longitude": record.longitude,
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
