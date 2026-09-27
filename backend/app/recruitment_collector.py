from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.care import care_recruitment_signal, classify_care_recruitment
from app.collector_events import collector_payload
from app.config import Settings
from app.logging import configure_logging
from app.queueing import SignalIngestionMessage, send_ingestion_message
from app.recruitment import (
    GovApprenticeshipProvider,
    RecruitmentProvider,
    RecruitmentQuery,
    classify_recruitment,
    recruitment_signal,
)
from app.secrets import provider_api_key_from_secret
from app.source_runs import finish_run, safe_failure, start_run
from app.verticals import CHILDRENS_HOME, NURSERY, VERTICAL_REGISTRY, validate_vertical

logger = configure_logging()


def _requested_verticals(event: dict[str, Any]) -> tuple[str, ...]:
    requested = event.get("verticals")
    if requested is None and event.get("vertical"):
        requested = [event["vertical"]]
    if requested is None:
        return tuple(
            key for key, definition in VERTICAL_REGISTRY.items() if definition.enabled
        )
    if not isinstance(requested, list) or not requested:
        raise ValueError("verticals must be a non-empty list")
    return tuple(dict.fromkeys(validate_vertical(str(value)) for value in requested))


def recruitment_discovery_queries(
    event: dict[str, Any], base: RecruitmentQuery
) -> tuple[RecruitmentQuery, ...]:
    """Build bounded provider queries from enabled vertical configuration."""
    requested = _requested_verticals(event)
    care_max_records = min(max(int(event.get("care_max_records", base.max_records)), 1), 250)
    queries: list[RecruitmentQuery] = []
    for vertical in requested:
        definition = VERTICAL_REGISTRY[vertical]
        if not definition.recruitment_routes:
            continue
        maximum = care_max_records if vertical == CHILDRENS_HOME else base.max_records
        route_slug = "-".join(definition.recruitment_routes).lower().replace(" ", "-")
        queries.append(
            replace(
                base,
                max_records=maximum,
                routes=definition.recruitment_routes,
                discovery_key=f"{vertical.lower()}-{route_slug}",
                discovery_verticals=(vertical,),
            )
        )
    return tuple(queries)


def _merge_discovery(existing: Any, incoming: Any) -> Any:
    return replace(
        existing,
        discovery_queries=tuple(
            dict.fromkeys((*existing.discovery_queries, *incoming.discovery_queries))
        ),
        discovery_verticals=tuple(
            dict.fromkeys((*existing.discovery_verticals, *incoming.discovery_verticals))
        ),
    )


def collect_recruitment(
    settings: Settings,
    event: dict[str, Any] | None = None,
    provider: RecruitmentProvider | None = None,
) -> dict[str, int]:
    event = event or {}
    query = RecruitmentQuery.from_event(event)
    discovery_queries = recruitment_discovery_queries(event, query)
    requested_verticals = _requested_verticals(event)
    source = str(event.get("source", "manual"))[:32]
    run_id, started_at = start_run(
        settings,
        source_key="recruitment",
        provider="GOV.UK Apprenticeships",
        invocation_source="scheduled" if source == "scheduled" else "manual",
        parameters={
            "posted_since_days": query.posted_since_days,
            "max_records": query.max_records,
            "page_size": query.page_size,
            "care_max_records": min(
                max(int(event.get("care_max_records", query.max_records)), 1), 250
            ),
            "queries": [item.discovery_key for item in discovery_queries],
        },
        run_id=str(event.get("run_id")) if event.get("run_id") else None,
        started_at=str(event.get("run_started_at")) if event.get("run_started_at") else None,
    )
    counts = {
        "records_fetched": 0,
        "unique_jobs": 0,
        "queries_executed": len(discovery_queries),
        "candidates_matched": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": 0,
        "nursery_matched": 0,
        "care_matched": 0,
        "care_relevant_change": 0,
        "care_relevant_routine": 0,
        "care_uncertain": 0,
    }
    status = "SUCCESS"
    failure_category = failure_message = None
    try:
        if provider is None:
            if not settings.recruitment_provider_secret_arn:
                raise RuntimeError("RECRUITMENT_PROVIDER_SECRET_ARN is not configured")
            api_key = provider_api_key_from_secret(settings.recruitment_provider_secret_arn)
            provider = GovApprenticeshipProvider(
                api_key, base_url=settings.recruitment_provider_base_url
            )
        if not settings.ingestion_queue_url:
            raise RuntimeError("INGESTION_QUEUE_URL is not configured")
        records: dict[tuple[str, str], Any] = {}
        for discovery_query in discovery_queries:
            for record in provider.vacancies(discovery_query):
                counts["records_fetched"] += 1
                identity = (record.provider, record.external_id)
                if identity in records:
                    records[identity] = _merge_discovery(records[identity], record)
                    counts["duplicates"] += 1
                else:
                    records[identity] = record
        counts["unique_jobs"] = len(records)
        for record in records.values():
            nursery_decision = classify_recruitment(record)
            care_decision = classify_care_recruitment(record)
            signals = []
            if NURSERY in requested_verticals and nursery_decision["matched"]:
                signals.append(recruitment_signal(record, nursery_decision))
                counts["nursery_matched"] += 1
            if CHILDRENS_HOME in requested_verticals and care_decision["matched"]:
                signals.append(care_recruitment_signal(record, care_decision))
                counts["care_matched"] += 1
                if care_decision["relevance"] == "RELEVANT_CHANGE":
                    counts["care_relevant_change"] += 1
                elif care_decision["relevance"] == "RELEVANT_ROUTINE":
                    counts["care_relevant_routine"] += 1
                else:
                    counts["care_uncertain"] += 1
            if not signals:
                counts["excluded"] += 1
                continue
            counts["candidates_matched"] += len(signals)
            for signal in signals:
                try:
                    body = json.dumps(
                        {
                            "message_version": "1.0",
                            "signal": signal,
                            "raw_provider_record": record.raw,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode()
                    if len(body) > 240_000:
                        raise ValueError("recruitment record is too large for SQS")
                    send_ingestion_message(settings, SignalIngestionMessage.from_json(body))
                    counts["signals_queued"] += 1
                except Exception:
                    counts["errors"] += 1
                    logger.exception(
                        "recruitment_record_queue_failed external_id=%s vertical=%s",
                        record.external_id,
                        signal.get("vertical", "NURSERY"),
                    )
    except Exception as exc:
        status = "FAILED"
        failure_category, failure_message = safe_failure(exc)
        counts["errors"] += 1
        logger.error(
            "recruitment_provider_failed error_type=%s error=%s",
            type(exc).__name__,
            str(exc)[:512],
        )
        raise
    finally:
        logger.info(
            "recruitment_collection_summary "
            "provider=govuk-apprenticeships fetched=%d unique=%d queries=%d matched=%d queued=%d "
            "duplicates=%d excluded=%d errors=%d posted_since_days=%d "
            "nursery_matched=%d care_matched=%d care_routine=%d care_change=%d "
            "care_uncertain=%d max_records=%d care_max_records=%d page_size=%d source=%s",
            counts["records_fetched"],
            counts["unique_jobs"],
            counts["queries_executed"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["excluded"],
            counts["errors"],
            query.posted_since_days,
            counts["nursery_matched"],
            counts["care_matched"],
            counts["care_relevant_routine"],
            counts["care_relevant_change"],
            counts["care_uncertain"],
            query.max_records,
            min(max(int(event.get("care_max_records", query.max_records)), 1), 250),
            query.page_size,
            event.get("source", "manual")[:32],
        )
        finish_run(
            settings,
            source_key="recruitment",
            run_id=run_id,
            started_at=started_at,
            status=status,
            counts=counts,
            failure_category=failure_category,
            failure_message=failure_message,
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_recruitment(Settings.from_env(), collector_payload(event))
