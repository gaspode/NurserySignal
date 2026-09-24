from __future__ import annotations

import json
import logging
from typing import Any

from app.config import Settings
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

logger = logging.getLogger("nurserysignal.collector")


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
    counts = {
        "records_fetched": 0,
        "candidates_matched": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "errors": 0,
    }
    for record in provider.applications(query):
        counts["records_fetched"] += 1
        decision: CandidateDecision = candidate_decision(record)
        if not decision.matched:
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
    logger.info("planning_collection_complete counts=%s", counts)
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_planning(Settings.from_env(), event)
