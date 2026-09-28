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
    provider_registered_name: str | None = None
    provider_registered_locality: str | None = None
    provider_registered_postcode: str | None = None
    provider_registered_address: str | None = None
    provider_registration_date: str | None = None
    aliases: tuple[str, ...] = ()


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


_UK_LEGAL_SUFFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("limited", "liability", "partnership"), "limited liability partnership"),
    (("public", "limited", "company"), "public limited company"),
    (("llp",), "limited liability partnership"),
    (("plc",), "public limited company"),
    (("ltd",), "limited"),
    (("limited",), "limited"),
)


def normalize_uk_legal_name(value: Any) -> str:
    """Canonicalise only recognised UK legal suffixes, preserving the name itself."""
    words = normalize_identity(value).split()
    for suffix, canonical in _UK_LEGAL_SUFFIXES:
        if tuple(words[-len(suffix) :]) == suffix:
            return " ".join((*words[: -len(suffix)], canonical)).strip()
    return " ".join(words)


def _name_without_legal_suffix(value: Any) -> str:
    words = normalize_identity(value).split()
    for suffix, _canonical in _UK_LEGAL_SUFFIXES:
        if tuple(words[-len(suffix) :]) == suffix:
            return " ".join(words[: -len(suffix)])
    return " ".join(words)


def _outward_postcode(value: Any) -> str:
    return str(value or "").strip().upper().replace(" ", "")[:-3]


def normalize_company_number(value: Any) -> str:
    """Return a safe Companies House lookup key or reject malformed input."""
    normalized = re.sub(r"[\s-]+", "", str(value or "")).upper()
    if not re.fullmatch(r"[A-Z0-9]{8}", normalized) or not any(
        character.isdigit() for character in normalized
    ):
        raise ValueError("invalid Companies House company number")
    return normalized


def _identity_names(candidate: OrganisationCandidate) -> tuple[str, ...]:
    values = (
        candidate.provider_registered_name,
        candidate.name,
        *candidate.aliases,
    )
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = normalize_uk_legal_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            names.append(normalized)
    return tuple(names)


def _compact_leading_name_variant(value: Any) -> str:
    """Handle public-provider/legal-name spacing differences without name-specific rules."""
    words = normalize_identity(value).split()
    if len(words) < 3:
        return ""
    return " ".join((f"{words[0]}{words[1]}", *words[2:]))


def _discovery_queries(candidate: OrganisationCandidate) -> tuple[tuple[str, str], ...]:
    values = (
        (candidate.name, "OBSERVED_PROVIDER_NAME"),
        (normalize_uk_legal_name(candidate.name), "NORMALIZED_OBSERVED_PROVIDER_NAME"),
        (candidate.provider_registered_name, "OFSTED_REGISTERED_PROVIDER_NAME"),
        (
            normalize_uk_legal_name(candidate.provider_registered_name),
            "NORMALIZED_OFSTED_REGISTERED_PROVIDER_NAME",
        ),
        (
            _compact_leading_name_variant(candidate.provider_registered_name),
            "OFSTED_REGISTERED_PROVIDER_NAME_VARIANT",
        ),
        *((alias, "APPROVED_ALIAS") for alias in candidate.aliases[:10]),
    )
    queries: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for raw_value, source in values:
        value = " ".join(str(raw_value or "").split()).strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen[key] = len(queries)
        queries.append((value, source))
    return tuple(queries[:8])


def compare_company_candidate(
    candidate: OrganisationCandidate, company: dict[str, Any]
) -> dict[str, Any]:
    identity_names = _identity_names(candidate) or (normalize_identity(candidate.name),)
    legal_name = normalize_uk_legal_name(company.get("company_name"))
    observed = max(
        identity_names,
        key=lambda value: SequenceMatcher(None, value, legal_name).ratio(),
    )
    similarity = max(
        SequenceMatcher(None, observed, legal_name).ratio(),
        SequenceMatcher(
            None,
            _name_without_legal_suffix(observed),
            _name_without_legal_suffix(legal_name),
        ).ratio(),
    )
    address = company.get("registered_office_address") or {}
    source_locality = normalize_identity(candidate.locality)
    company_locality = normalize_identity(address.get("locality"))
    locality_agrees = bool(source_locality and source_locality == company_locality)
    source_outward = _outward_postcode(candidate.postcode)
    company_outward = _outward_postcode(address.get("postal_code"))
    postcode_area_agrees = bool(source_outward and source_outward == company_outward)
    provider_locality = normalize_identity(candidate.provider_registered_locality)
    provider_postcode = str(candidate.provider_registered_postcode or "").upper().replace(
        " ", ""
    )
    company_postcode = str(address.get("postal_code") or "").upper().replace(" ", "")
    provider_locality_agrees = bool(
        provider_locality and provider_locality == company_locality
    )
    provider_postcode_agrees = bool(
        provider_postcode and provider_postcode == company_postcode
    )
    provider_address = normalize_identity(candidate.provider_registered_address)
    company_address_line = normalize_identity(address.get("address_line_1"))
    provider_address_agrees = bool(
        provider_postcode_agrees
        and company_address_line
        and (
            company_address_line == provider_address
            or company_address_line in provider_address
        )
    )
    exact_normalized_legal_name = observed == legal_name
    reasons: list[str] = []
    cautions: list[str] = []
    match_features: list[str] = []
    if exact_normalized_legal_name:
        reasons.append("Exact normalized legal-name match")
        match_features.append("EXACT_NORMALIZED_LEGAL_NAME")
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
    if provider_locality_agrees:
        reasons.append("Provider-office locality agrees")
    elif provider_locality and company_locality:
        cautions.append("Companies House office town differs from Ofsted provider address")
    if provider_postcode_agrees:
        reasons.append("Exact Ofsted provider-office postcode match")
        match_features.append("EXACT_OFSTED_PROVIDER_OFFICE_POSTCODE")
    elif provider_postcode and company_postcode:
        cautions.append("Companies House office postcode differs from Ofsted provider office")
    incorporation_compatible = None
    if candidate.provider_registration_date and company.get("date_of_creation"):
        incorporation_compatible = str(company["date_of_creation"]) <= str(
            candidate.provider_registration_date
        )
        if incorporation_compatible:
            reasons.append("Company existed before Ofsted registration")
        else:
            cautions.append("Company incorporated after Ofsted registration")
    if provider_address_agrees:
        reasons.append("Provider-office address agrees")
        match_features.append("OFSTED_PROVIDER_OFFICE_ADDRESS_AGREES")
    if not any(
        (
            locality_agrees,
            postcode_area_agrees,
            provider_locality_agrees,
            provider_postcode_agrees,
        )
    ):
        cautions.append("Name match only; no location corroboration")
    outcome = "PROBABLE" if similarity >= 0.9 else "UNCERTAIN"
    if exact_normalized_legal_name and (
        locality_agrees
        or postcode_area_agrees
        or provider_locality_agrees
        or provider_postcode_agrees
    ):
        outcome = "STRONG"
    return {
        **company,
        "match_outcome": outcome,
        "name_similarity": round(similarity, 4),
        "match_reasons": reasons,
        "match_cautions": cautions,
        "match_features": match_features,
        "location_agreement": {
            "locality": locality_agrees,
            "postcode_area": postcode_area_agrees,
            "ofsted_provider_locality": provider_locality_agrees,
            "ofsted_provider_postcode": provider_postcode_agrees,
            "ofsted_provider_address": provider_address_agrees,
            "incorporation_timing_compatible": incorporation_compatible,
        },
    }


def _candidate_support(item: dict[str, Any]) -> int:
    agreement = item.get("location_agreement") or {}
    timing = agreement.get("incorporation_timing_compatible")
    return (
        (4 if "EXACT_NORMALIZED_LEGAL_NAME" in item.get("match_features", []) else 0)
        + (4 if agreement.get("ofsted_provider_postcode") else 0)
        + (2 if agreement.get("ofsted_provider_address") else 0)
        + (1 if agreement.get("ofsted_provider_locality") else 0)
        + (1 if timing is True else (-1 if timing is False else 0))
    )


def _candidate_rank(item: dict[str, Any]) -> tuple[int, int, int, int, int, float, str]:
    agreement = item.get("location_agreement") or {}
    timing = agreement.get("incorporation_timing_compatible")
    return (
        _candidate_support(item),
        1 if "EXACT_NORMALIZED_LEGAL_NAME" in item.get("match_features", []) else 0,
        1 if agreement.get("ofsted_provider_postcode") else 0,
        1 if item.get("match_outcome") == "STRONG" else 0,
        1 if timing is True else (-1 if timing is False else 0),
        float(item.get("name_similarity") or 0),
        str(item.get("company_number") or ""),
    )


def rank_company_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank explainably and flag a materially better candidate without resolving it."""
    ranked = sorted(items, key=_candidate_rank, reverse=True)

    scores = [_candidate_support(item) for item in ranked]
    materially_better = bool(
        ranked and scores[0] >= 4 and (len(scores) == 1 or scores[0] - scores[1] >= 2)
    )
    return [
        {**item, "best_supported_match": index == 0 and materially_better}
        for index, item in enumerate(ranked)
    ]


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

    def discover(self, candidate: OrganisationCandidate) -> tuple[dict[str, Any], ...]:
        """Search every bounded identity name and merge results by company number."""
        merged: dict[str, dict[str, Any]] = {}
        for query, source in _discovery_queries(candidate):
            for item in self.search(query, limit=10):
                company_number = str(item.get("company_number") or "").strip().upper()
                if not company_number:
                    continue
                existing = merged.get(company_number, {})
                discovery_sources = sorted(
                    {
                        *existing.get("discovery_sources", []),
                        source,
                    }
                )
                discovery_queries = sorted(
                    {
                        *existing.get("discovery_queries", []),
                        query,
                    }
                )
                merged[company_number] = {
                    **existing,
                    **{
                        key: value
                        for key, value in item.items()
                        if value not in (None, "", [], {})
                    },
                    "company_number": company_number,
                    "discovery_sources": discovery_sources,
                    "discovery_queries": discovery_queries,
                }
        identity_names = _identity_names(candidate)
        ordered = sorted(
            merged.values(),
            key=lambda item: max(
                (
                    SequenceMatcher(
                        None,
                        identity_name,
                        normalize_uk_legal_name(item.get("company_name")),
                    ).ratio()
                    for identity_name in identity_names
                ),
                default=0,
            ),
            reverse=True,
        )
        return tuple(ordered[:10])

    def _detailed_candidate(self, item: dict[str, Any]) -> dict[str, Any]:
        company = item
        company_number = str(item.get("company_number") or "")
        if company_number:
            try:
                company = self.company_profile(company_number)
            except CompaniesHouseError:
                # The search result still gives an admin a safe bounded fallback.
                company = item
        return {
            **company,
            "discovery_sources": item.get("discovery_sources") or [],
            "discovery_queries": item.get("discovery_queries") or [],
        }

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
        lookup_name = candidate.provider_registered_name or candidate.name
        results = self.discover(candidate)
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
        target = normalize_uk_legal_name(lookup_name)
        exact = [
            item
            for item in results
            if normalize_uk_legal_name(item.get("company_name")) == target
        ]
        if len(exact) == 1:
            compared_exact = rank_company_candidates(
                [compare_company_candidate(candidate, exact[0])]
            )[0]
            ofsted_location_agrees = any(
                (compared_exact.get("location_agreement") or {}).get(key)
                for key in ("ofsted_provider_locality", "ofsted_provider_postcode")
            )
            profile = self._detailed_candidate(exact[0])
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "STRONG",
                0.99 if ofsted_location_agrees else 0.95,
                (
                    "unique registered-provider legal-name match corroborated by "
                    "Ofsted provider address"
                    if ofsted_location_agrees
                    else "unique normalized legal-name match"
                ),
                profile,
                (compared_exact,),
            )
        detailed = [self._detailed_candidate(item) for item in results]
        compared = [compare_company_candidate(candidate, item) for item in detailed]
        ranked_compared = rank_company_candidates(compared)
        provider_corroborated = [
            item
            for item in compared
            if item.get("match_outcome") == "STRONG"
            and any(
                (item.get("location_agreement") or {}).get(key)
                for key in ("ofsted_provider_locality", "ofsted_provider_postcode")
            )
        ]
        if len(provider_corroborated) == 1:
            selected = provider_corroborated[0]
            profile = self._detailed_candidate(selected)
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "STRONG",
                0.98,
                "unique legal-name match corroborated by Ofsted provider address",
                profile,
                tuple(ranked_compared),
            )
        locality = normalize_identity(candidate.locality)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in results:
            score = SequenceMatcher(
                None, target, normalize_uk_legal_name(item.get("company_name"))
            ).ratio()
            address = item.get("registered_office_address") or {}
            if locality and locality == normalize_identity(address.get("locality")):
                score += 0.05
            scored.append((min(score, 1.0), item))
        scored.sort(key=lambda value: value[0], reverse=True)
        best_score, best = scored[0]
        if best_score >= 0.94 and (len(scored) == 1 or best_score - scored[1][0] >= 0.08):
            profile = self._detailed_candidate(best)
            return CompanyResolution(
                candidate.operator_id,
                candidate.name,
                "MATCHED",
                "STRONG",
                best_score,
                "strong legal-name match corroborated by a clear search lead",
                profile,
                tuple(ranked_compared),
            )
        detailed_candidates = ranked_compared
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
