from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import boto3

from app.collector_events import collector_payload
from app.companies_house import (
    CompaniesHouseError,
    CompaniesHouseProvider,
    OrganisationCandidate,
    normalize_company_number,
)
from app.config import Settings
from app.logging import configure_logging
from app.organisation_types import is_public_authority_name
from app.secrets import provider_api_key_from_secret
from app.source_runs import finish_run, safe_failure, start_run

logger = configure_logging()


def collect_companies_house(
    settings: Settings,
    event: dict[str, Any] | None = None,
    provider: CompaniesHouseProvider | None = None,
) -> dict[str, int]:
    event = event or {}
    raw_candidates = event.get("organisation_candidates") or []
    if not isinstance(raw_candidates, list):
        raise ValueError("organisation_candidates must be a list")
    limit = min(max(int(event.get("max_organisations", 10)), 1), 25)
    candidates = [
        OrganisationCandidate(
            operator_id=str(item["operator_id"]),
            name=str(item["name"]),
            company_number=str(item["company_number"]) if item.get("company_number") else None,
            locality=str(item["locality"]) if item.get("locality") else None,
            postcode=str(item["postcode"]) if item.get("postcode") else None,
            address=str(item["address"]) if item.get("address") else None,
            provider_registered_name=(
                str(item["provider_registered_name"])
                if item.get("provider_registered_name")
                else None
            ),
            provider_registered_locality=(
                str(item["provider_registered_locality"])
                if item.get("provider_registered_locality")
                else None
            ),
            provider_registered_postcode=(
                str(item["provider_registered_postcode"])
                if item.get("provider_registered_postcode")
                else None
            ),
            provider_registered_address=(
                str(item["provider_registered_address"])
                if item.get("provider_registered_address")
                else None
            ),
            provider_registration_date=(
                str(item["provider_registration_date"])
                if item.get("provider_registration_date")
                else None
            ),
            aliases=tuple(
                str(alias).strip()
                for alias in (item.get("aliases") or [])[:10]
                if str(alias).strip()
            ),
        )
        for item in raw_candidates[:limit]
        if isinstance(item, dict)
        and item.get("operator_id")
        and item.get("name")
        and not (
            item.get("organisation_type") != "PRIVATE_COMPANY"
            and is_public_authority_name(item.get("name"))
        )
    ]
    run_id, started_at = start_run(
        settings,
        source_key="companies_house",
        provider="Companies House",
        invocation_source="manual",
        parameters={"max_organisations": limit, "candidate_count": len(candidates)},
        run_id=str(event.get("run_id")) if event.get("run_id") else None,
        started_at=str(event.get("run_started_at")) if event.get("run_started_at") else None,
    )
    counts = {
        "records_fetched": 0,
        "organisations_attempted": 0,
        "exact_or_strong": 0,
        "ambiguous": 0,
        "no_match": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "errors": 0,
    }
    status = "SUCCESS"
    failure_category = failure_message = None
    try:
        if not candidates:
            return counts
        if not settings.companies_house_secret_arn:
            raise RuntimeError("COMPANIES_HOUSE_SECRET_ARN is not configured")
        if not settings.ingestion_queue_url:
            raise RuntimeError("INGESTION_QUEUE_URL is not configured")
        provider = provider or CompaniesHouseProvider(
            provider_api_key_from_secret(settings.companies_house_secret_arn),
            base_url=settings.companies_house_base_url,
        )
        queue = boto3.client("sqs")
        for candidate in candidates:
            counts["organisations_attempted"] += 1
            try:
                result = provider.resolve(candidate)
                counts["records_fetched"] += 1
                if result.status == "MATCHED":
                    counts["exact_or_strong"] += 1
                elif result.status == "AMBIGUOUS":
                    counts["ambiguous"] += 1
                else:
                    counts["no_match"] += 1
                body = json.dumps(
                    {
                        "message_type": "organisation_enrichment",
                        "provider": "COMPANIES_HOUSE",
                        "operator_id": result.operator_id,
                        "query_name": result.query_name,
                        "status": result.status,
                        "outcome": result.outcome,
                        "confidence": result.confidence,
                        "reason": result.reason,
                        "company": result.company,
                        "candidates": list(result.candidates),
                        "retrieved_at": datetime.now(UTC).isoformat(),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                )
                response = queue.send_message(
                    QueueUrl=settings.ingestion_queue_url, MessageBody=body
                )
                if not response.get("MessageId"):
                    raise RuntimeError("organisation enrichment message was not accepted")
                counts["signals_queued"] += 1
            except Exception:
                counts["errors"] += 1
                logger.exception(
                    "companies_house_candidate_failed operator_id=%s", candidate.operator_id
                )
    except Exception as exc:
        status = "FAILED"
        failure_category, failure_message = safe_failure(exc)
        counts["errors"] += 1
        raise
    finally:
        logger.info(
            "companies_house_collection_summary attempted=%d fetched=%d exact_or_strong=%d "
            "ambiguous=%d no_match=%d queued=%d duplicates=%d errors=%d max_organisations=%d",
            counts["organisations_attempted"],
            counts["records_fetched"],
            counts["exact_or_strong"],
            counts["ambiguous"],
            counts["no_match"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["errors"],
            limit,
        )
        finish_run(
            settings,
            source_key="companies_house",
            run_id=run_id,
            started_at=started_at,
            status=status,
            counts=counts,
            failure_category=failure_category,
            failure_message=failure_message,
        )
    return counts


def lookup_company_profile(settings: Settings, event: dict[str, Any]) -> dict[str, Any]:
    company_number = normalize_company_number(event.get("company_number"))
    if not settings.companies_house_secret_arn:
        raise RuntimeError("COMPANIES_HOUSE_SECRET_ARN is not configured")
    provider = CompaniesHouseProvider(
        provider_api_key_from_secret(settings.companies_house_secret_arn),
        base_url=settings.companies_house_base_url,
        timeout=8,
    )
    try:
        return {"status": "FOUND", "company": provider.company_profile(company_number)}
    except CompaniesHouseError as exc:
        if exc.status_code == 404:
            return {"status": "NOT_FOUND"}
        if exc.status_code == 429:
            return {"status": "RATE_LIMITED"}
        raise


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("operation") == "company_profile_lookup":
        return lookup_company_profile(Settings.from_env(), event)
    return collect_companies_house(Settings.from_env(), collector_payload(event))
