from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from app.correlation import normalize_identity

USER_AGENT = "SignalHub/1.0"
MAX_ERROR_BODY = 512
SIC_DESCRIPTIONS = {
    "68209": "Other letting and operating of own or leased real estate",
    "85100": "Pre-primary education",
    "85200": "Primary education",
    "85590": "Other education not elsewhere classified",
    "85600": "Educational support services",
    "87100": "Residential nursing care facilities",
    "87200": "Residential care for learning difficulties, mental health and substance abuse",
    "87300": "Residential care for elderly and disabled people",
    "87900": "Other residential care activities not elsewhere classified",
    "88100": "Social work without accommodation for elderly and disabled people",
    "88910": "Child day-care activities",
    "88990": "Other social work activities without accommodation not elsewhere classified",
    "96090": "Other service activities not elsewhere classified",
}


class CompaniesHouseError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CompaniesHouseRateLimitError(CompaniesHouseError):
    pass


@dataclass(frozen=True)
class OrganisationCandidate:
    operator_id: str
    name: str
    company_number: str | None = None
    locality: str | None = None
    postcode: str | None = None
    address: str | None = None


@dataclass(frozen=True)
class CompanyResolution:
    operator_id: str
    query_name: str
    status: str
    outcome: str
    confidence: float
    reason: str
    company: dict[str, Any] | None
    candidates: tuple[dict[str, Any], ...]


def _safe_error_body(raw: bytes, api_key: str) -> str:
    text = raw.decode("utf-8", errors="replace")[:MAX_ERROR_BODY]
    text = text.replace(api_key, "[REDACTED]")
    return re.sub(
        r"(?i)(authorization|api[_-]?key)(\s*[=:]\s*)[^,;\s}\"]+",
        r"\1\2[REDACTED]",
        text,
    )


def _safe_company(item: dict[str, Any]) -> dict[str, Any]:
    address = item.get("registered_office_address") or item.get("address") or {}
    company_number = item.get("company_number")
    sic_codes = item.get("sic_codes") or []
    return {
        "company_number": item.get("company_number"),
        "company_name": item.get("company_name") or item.get("title"),
        "company_status": item.get("company_status"),
        "type": item.get("type") or item.get("company_type"),
        "date_of_creation": item.get("date_of_creation"),
        "registered_office_address": {
            key: address.get(key)
            for key in (
                "address_line_1",
                "address_line_2",
                "locality",
                "region",
                "postal_code",
                "country",
            )
            if address.get(key)
        },
        "sic_codes": sic_codes,
        "sic_descriptions": [
            {"code": code, "description": SIC_DESCRIPTIONS.get(str(code))}
            for code in sic_codes
        ],
        "links": item.get("links") or {},
        "companies_house_url": (
            "https://find-and-update.company-information.service.gov.uk/company/"
            f"{company_number}"
            if company_number
            else None
        ),
    }


def _name_without_legal_suffix(value: Any) -> str:
    words = normalize_identity(value).split()
    while words and words[-1] in {"ltd", "limited", "plc", "llp"}:
        words.pop()
    return " ".join(words)


def _outward_postcode(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")[:-3]


def _candidate_comparison(
    candidate: OrganisationCandidate, company: dict[str, Any]
) -> dict[str, Any]:
    observed = normalize_identity(candidate.name)
    legal_name = normalize_identity(company.get("company_name"))
    similarity = SequenceMatcher(None, observed, legal_name).ratio()
    address = company.get("registered_office_address") or {}
    source_locality = normalize_identity(candidate.locality)
    company_locality = normalize_identity(address.get("locality"))
    locality_agrees = bool(source_locality and source_locality == company_locality)
    source_outward = _outward_postcode(candidate.postcode)
    company_outward = _outward_postcode(address.get("postal_code"))
    postcode_area_agrees = bool(source_outward and source_outward == company_outward)
    reasons: list[str] = []
    cautions: list[str] = []
    if observed == legal_name:
        reasons.append("Exact normalised legal-name match")
    elif _name_without_legal_suffix(observed) == _name_without_legal_suffix(legal_name):
        reasons.append("Legal name differs only by company suffix")
    elif similarity >= 0.9:
        reasons.append("Strong legal-name similarity")
    elif similarity >= 0.75:
        reasons.append("Partial legal-name similarity")
    else:
        cautions.append("Weak legal-name similarity")
    if locality_agrees:
        reasons.append("Same town/locality as source evidence")
    elif source_locality and company_locality:
        cautions.append("Registered-office town differs from source context")
    if postcode_area_agrees:
        reasons.append("Registered-office postcode area agrees with source evidence")
    elif source_outward and company_outward:
        cautions.append("Registered-office postcode area differs from source context")
    if not locality_agrees and not postcode_area_agrees:
        cautions.append("Name match only; no location corroboration")
    suffix_equivalent = _name_without_legal_suffix(observed) == _name_without_legal_suffix(
        legal_name
    )
    outcome = "PROBABLE" if similarity >= 0.9 or suffix_equivalent else "UNCERTAIN"
    if (observed == legal_name or suffix_equivalent) and (
        locality_agrees or postcode_area_agrees
    ):
        outcome = "STRONG"
    return {
        **company,
        "match_outcome": outcome,
        "name_similarity": round(similarity, 4),
        "match_reasons": reasons,
        "match_cautions": cautions,
        "location_agreement": {
            "locality": locality_agrees,
            "postcode_area": postcode_area_agrees,
        },
    }


class CompaniesHouseProvider:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.company-information.service.gov.uk",
        timeout: float = 15,
        opener: Any = urlopen,
        sleep: Any = time.sleep,
    ) -> None:
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ValueError("Companies House API key is empty")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener
        self.sleep = sleep

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        auth = base64.b64encode(f"{self.api_key}:".encode()).decode()
        request = Request(
            f"{self.base_url}{path}{query}",
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {auth}",
                "User-Agent": USER_AGENT,
            },
        )
        for attempt in range(3):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read())
                if not isinstance(payload, dict):
                    raise CompaniesHouseError("Companies House returned a non-object response")
                return payload
            except HTTPError as exc:
                detail = _safe_error_body(exc.read(MAX_ERROR_BODY), self.api_key)
                if exc.code == 429:
                    if attempt < 2:
                        self.sleep(0.5 * (2**attempt))
                        continue
                    raise CompaniesHouseRateLimitError(
                        f"Companies House HTTP 429: {detail}", status_code=429
                    ) from exc
                raise CompaniesHouseError(
                    f"Companies House HTTP {exc.code}: {detail}", status_code=exc.code
                ) from exc
            except (TimeoutError, URLError) as exc:
                if attempt < 2:
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise CompaniesHouseError("Companies House request failed") from exc
        raise CompaniesHouseError("Companies House request failed")

    def company_profile(self, company_number: str) -> dict[str, Any]:
        return _safe_company(self._get(f"/company/{quote(company_number, safe='')}"))

    def search(self, name: str, *, limit: int = 5) -> tuple[dict[str, Any], ...]:
        payload = self._get("/search/companies", {"q": name, "items_per_page": min(limit, 10)})
        return tuple(
            _safe_company(item)
            for item in payload.get("items", [])
            if isinstance(item, dict)
        )

    def resolve(self, candidate: OrganisationCandidate) -> CompanyResolution:
        if candidate.company_number:
            profile = self.company_profile(candidate.company_number)
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "EXACT",
                1.0,
                "existing company number resolved to an official company profile",
                profile,
                (),
            )
        results = self.search(candidate.name)
        if not results:
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "NO_MATCH",
                "NO_MATCH",
                0.0,
                "no Companies House search result",
                None,
                (),
            )
        target = normalize_identity(candidate.name)
        exact = [item for item in results if normalize_identity(item.get("company_name")) == target]
        if len(exact) == 1:
            profile = self.company_profile(str(exact[0]["company_number"]))
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "STRONG",
                0.95,
                "unique normalized legal-name match",
                profile,
                results,
            )
        locality = normalize_identity(candidate.locality)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in results:
            score = SequenceMatcher(
                None, target, normalize_identity(item.get("company_name"))
            ).ratio()
            address = item.get("registered_office_address") or {}
            if locality and locality == normalize_identity(address.get("locality")):
                score += 0.05
            scored.append((min(score, 1.0), item))
        scored.sort(key=lambda value: value[0], reverse=True)
        best_score, best = scored[0]
        if best_score >= 0.94 and (len(scored) == 1 or best_score - scored[1][0] >= 0.08):
            profile = self.company_profile(str(best["company_number"]))
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "STRONG",
                best_score,
                "strong legal-name match corroborated by a clear search lead",
                profile,
                results,
            )
        detailed_candidates: list[dict[str, Any]] = []
        for item in results:
            company = item
            company_number = str(item.get("company_number") or "")
            if company_number:
                try:
                    company = self.company_profile(company_number)
                except CompaniesHouseError:
                    # The search result still gives an admin a safe bounded fallback.
                    company = item
            detailed_candidates.append(_candidate_comparison(candidate, company))
        detailed_candidates.sort(
            key=lambda item: float(item.get("name_similarity") or 0), reverse=True
        )
        return CompanyResolution(
            candidate.operator_id,
            candidate.name,
            "AMBIGUOUS",
            "UNCERTAIN",
            best_score,
            "multiple or insufficiently corroborated company candidates require admin review",
            None,
            tuple(detailed_candidates),
        )
