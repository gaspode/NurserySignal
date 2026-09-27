from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from app.care import care_planning_signal, classify_care_planning
from app.collector_events import collector_payload
from app.config import Settings
from app.logging import configure_logging
from app.planning import (
    CandidateDecision,
    PlanningProvider,
    PlanningQuery,
    PlotaProvider,
    candidate_decision,
    planning_signal,
)
from app.queueing import SignalIngestionMessage, send_ingestion_message
from app.secrets import provider_api_key_from_secret
from app.source_runs import finish_run, safe_failure, start_run
from app.verticals import CHILDRENS_HOME, NURSERY, VERTICAL_REGISTRY, validate_vertical

logger = configure_logging()


def _requested_verticals(event: dict[str, Any]) -> tuple[str, ...]:
    requested = event.get("verticals")
    if requested is None:
        return tuple(key for key, item in VERTICAL_REGISTRY.items() if item.enabled)
    if not isinstance(requested, list) or not requested:
        raise ValueError("verticals must be a non-empty list")
    return tuple(dict.fromkeys(validate_vertical(str(value)) for value in requested))


def collect_planning(
    settings: Settings,
    event: dict[str, Any] | None = None,
    provider: PlanningProvider | None = None,
) -> dict[str, int]:
    event = event or {}
    query = PlanningQuery.from_event(event)
    requested_verticals = _requested_verticals(event)
    source = str(event.get("source", "manual"))[:32]
    lookback_days = min(max(int(event.get("lookback_days", 2)), 1), 31)
    care_max_records = min(max(int(event.get("care_max_records", 50)), 1), 100)
    run_id, started_at = start_run(
        settings,
        source_key="planning",
        provider="Plota",
        invocation_source="scheduled" if source == "scheduled" else "manual",
        parameters={
            "lookback_days": lookback_days,
            "max_records": query.max_records,
            "page_size": query.page_size,
            "care_max_records": care_max_records,
        },
        run_id=str(event.get("run_id")) if event.get("run_id") else None,
        started_at=str(event.get("run_started_at")) if event.get("run_started_at") else None,
    )
    counts = {
        "records_fetched": 0,
        "candidates_matched": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": 0,
        "nursery_matched": 0,
        "care_matched": 0,
    }
    status = "SUCCESS"
    failure_category = failure_message = None
    try:
        if provider is None:
            if not settings.planning_provider_secret_arn:
                raise RuntimeError("PLANNING_PROVIDER_SECRET_ARN is not configured")
            api_key = provider_api_key_from_secret(settings.planning_provider_secret_arn)
            provider = PlotaProvider(api_key, base_url=settings.planning_provider_base_url)
        if not settings.ingestion_queue_url:
            raise RuntimeError("INGESTION_QUEUE_URL is not configured")
        queries = (
            [query]
            if event.get("search_term")
            else [
                query,
                replace(
                    query,
                    search_term="children's home",
                    max_records=(care_max_records + 1) // 2,
                ),
                replace(
                    query,
                    search_term="childrens home",
                    max_records=care_max_records // 2,
                ),
            ]
        )
        seen_records: set[tuple[str, str]] = set()
        for provider_query in queries:
            for record in provider.applications(provider_query):
                record_key = (record.application_id, record.description)
                if record_key in seen_records:
                    continue
                seen_records.add(record_key)
                counts["records_fetched"] += 1
                nursery_decision: CandidateDecision = candidate_decision(record)
                care_decision = classify_care_planning(record)
                signals = []
                if NURSERY in requested_verticals and nursery_decision.matched:
                    signals.append(planning_signal(record, nursery_decision))
                    counts["nursery_matched"] += 1
                if CHILDRENS_HOME in requested_verticals and care_decision.matched:
                    signals.append(care_planning_signal(record, care_decision))
                    counts["care_matched"] += 1
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
                            raise ValueError("planning record is too large for SQS")
                        send_ingestion_message(settings, SignalIngestionMessage.from_json(body))
                        counts["signals_queued"] += 1
                    except Exception:
                        counts["errors"] += 1
                        logger.exception(
                            "planning_record_queue_failed application_id=%s vertical=%s",
                            record.application_id,
                            signal.get("vertical", "NURSERY"),
                        )
    except Exception as exc:
        status = "FAILED"
        failure_category, failure_message = safe_failure(exc)
        counts["errors"] += 1
        raise
    finally:
        logger.info(
            "planning_collection_summary fetched=%d matched=%d queued=%d duplicates=%d "
            "excluded=%d errors=%d nursery_matched=%d care_matched=%d "
            "lookback_days=%d max_records=%d care_max_records=%d page_size=%d source=%s",
            counts["records_fetched"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["excluded"],
            counts["errors"],
            counts["nursery_matched"],
            counts["care_matched"],
            lookback_days,
            query.max_records,
            care_max_records,
            query.page_size,
            source,
        )
        finish_run(
            settings,
            source_key="planning",
            run_id=run_id,
            started_at=started_at,
            status=status,
            counts=counts,
            failure_category=failure_category,
            failure_message=failure_message,
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_planning(Settings.from_env(), collector_payload(event))
