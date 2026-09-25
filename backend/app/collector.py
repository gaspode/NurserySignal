from __future__ import annotations

import json
from typing import Any

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

logger = configure_logging()


def collect_planning(
    settings: Settings,
    event: dict[str, Any] | None = None,
    provider: PlanningProvider | None = None,
) -> dict[str, int]:
    if provider is None:
        if not settings.planning_provider_secret_arn:
            raise RuntimeError("PLANNING_PROVIDER_SECRET_ARN is not configured")
        api_key = provider_api_key_from_secret(settings.planning_provider_secret_arn)
        provider = PlotaProvider(api_key, base_url=settings.planning_provider_base_url)
    if not settings.ingestion_queue_url:
        raise RuntimeError("INGESTION_QUEUE_URL is not configured")
    query = PlanningQuery.from_event(event)
    event = event or {}
    source = str(event.get("source", "manual"))[:32]
    lookback_days = min(max(int(event.get("lookback_days", 2)), 1), 31)
    counts = {
        "records_fetched": 0,
        "candidates_matched": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": 0,
    }
    try:
        for record in provider.applications(query):
            counts["records_fetched"] += 1
            decision: CandidateDecision = candidate_decision(record)
            if not decision.matched:
                counts["excluded"] += 1
                continue
            counts["candidates_matched"] += 1
            try:
                signal = planning_signal(record, decision)
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
                    "planning_record_queue_failed application_id=%s", record.application_id
                )
    except Exception:
        counts["errors"] += 1
        raise
    finally:
        logger.info(
            "planning_collection_summary fetched=%d matched=%d queued=%d duplicates=%d "
            "excluded=%d errors=%d lookback_days=%d max_records=%d page_size=%d source=%s",
            counts["records_fetched"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["excluded"],
            counts["errors"],
            lookback_days,
            query.max_records,
            query.page_size,
            source,
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_planning(Settings.from_env(), event)
