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
        "sic_codes": item.get("sic_codes") or [],
        "links": item.get("links") or {},
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
        return CompanyResolution(
            candidate.operator_id,
            candidate.name,
            "AMBIGUOUS",
            "UNCERTAIN",
            best_score,
            "multiple or insufficiently corroborated company candidates require admin review",
            None,
            results,
        )
