from __future__ import annotations

import json
from typing import Any

import boto3

from app.companies_house import (
    OrganisationCandidate,
    compare_company_candidate,
    normalize_company_number,
)
from app.config import Settings
from app.repository import (
    add_manual_organisation_candidate,
    list_organisation_match_reviews,
)


class ManualCompanyLookupError(RuntimeError):
    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _review_candidate(review: dict[str, Any]) -> OrganisationCandidate:
    context = review.get("source_context") or {}
    ofsted = next(iter(context.get("ofsted_evidence") or []), {})
    signal = next(iter(context.get("signals") or []), {})
    return OrganisationCandidate(
        operator_id=str(review["operator_id"]),
        name=str(context.get("observed_name") or review.get("organisation_name") or ""),
        locality=signal.get("town") or signal.get("local_authority"),
        postcode=signal.get("postcode"),
        address=signal.get("address"),
        provider_registered_name=ofsted.get("registered_provider_name"),
        provider_registered_locality=ofsted.get("provider_registered_locality"),
        provider_registered_postcode=ofsted.get("provider_registered_postcode"),
        provider_registered_address=ofsted.get("provider_registered_address"),
        provider_registration_date=(
            str(ofsted["registration_date"]) if ofsted.get("registration_date") else None
        ),
    )


def lookup_manual_company_candidate(
    settings: Settings,
    review_id: str,
    company_number: Any,
    *,
    lambda_client: Any | None = None,
) -> dict[str, Any]:
    try:
        normalized = normalize_company_number(company_number)
    except ValueError as exc:
        raise ManualCompanyLookupError("invalid_company_number", 400) from exc
    if not settings.companies_house_lookup_function_name:
        raise ManualCompanyLookupError("company_lookup_not_configured", 503)
    reviews = list_organisation_match_reviews(settings, limit=100)
    review = next((item for item in reviews if str(item.get("id")) == review_id), None)
    if not review:
        raise ManualCompanyLookupError("organisation_review_not_found", 404)
    client = lambda_client or boto3.client("lambda")
    response = client.invoke(
        FunctionName=settings.companies_house_lookup_function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {"operation": "company_profile_lookup", "company_number": normalized}
        ).encode(),
    )
    if response.get("FunctionError"):
        raise ManualCompanyLookupError("company_lookup_failed", 502)
    raw_payload = response.get("Payload")
    raw = raw_payload.read() if hasattr(raw_payload, "read") else raw_payload
    try:
        result = json.loads(raw or b"{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ManualCompanyLookupError("company_lookup_failed", 502) from exc
    status = result.get("status")
    if status == "NOT_FOUND":
        raise ManualCompanyLookupError("company_not_found", 404)
    if status == "RATE_LIMITED":
        raise ManualCompanyLookupError("company_lookup_rate_limited", 429)
    company = result.get("company")
    if status != "FOUND" or not isinstance(company, dict):
        raise ManualCompanyLookupError("company_lookup_failed", 502)
    compared = compare_company_candidate(_review_candidate(review), company)
    compared["selection_source"] = "MANUAL_LOOKUP"
    return add_manual_organisation_candidate(
        settings,
        review_id,
        candidate=compared,
    )
