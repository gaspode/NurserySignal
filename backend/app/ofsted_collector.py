from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.collector_events import collector_payload
from app.config import Settings
from app.logging import configure_logging
from app.ofsted import (
    OfstedQuery,
    download_register,
    ofsted_signal,
    records_from_ods,
)
from app.queueing import SignalIngestionMessage, send_ingestion_message
from app.source_runs import finish_run, safe_failure, start_run

logger = configure_logging()


def collect_ofsted(settings: Settings, event: dict[str, Any] | None = None) -> dict[str, int]:
    event = event or {}
    query = OfstedQuery.from_event(event)
    source = str(event.get("source", "manual"))[:32]
    run_id, started_at = start_run(
        settings,
        source_key="ofsted",
        provider="Ofsted",
        invocation_source="manual",
        parameters={
            "registered_since_days": query.registered_since_days,
            "max_records": query.max_records,
            "active_only": query.active_only,
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
    }
    status = "SUCCESS"
    failure_category = failure_message = None
    register_path = None
    try:
        if not settings.ingestion_queue_url:
            raise RuntimeError("INGESTION_QUEUE_URL is not configured")
        register_path = download_register(settings.ofsted_data_url)
        retrieved_at = datetime.now(UTC)
        for record in records_from_ods(register_path, query):
            counts["records_fetched"] += 1
            signal = ofsted_signal(record, retrieved_at=retrieved_at)
            body = json.dumps(
                {
                    "message_version": "1.0",
                    "signal": signal,
                    "raw_provider_record": {
                        **record.raw,
                        "dataset_url": settings.ofsted_data_url,
                        "retrieved_at": retrieved_at.isoformat(),
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            if len(body) > 240_000:
                raise ValueError("Ofsted record is too large for SQS")
            send_ingestion_message(settings, SignalIngestionMessage.from_json(body))
            counts["candidates_matched"] += 1
            counts["signals_queued"] += 1
    except Exception as exc:
        status = "FAILED"
        failure_category, failure_message = safe_failure(exc)
        counts["errors"] += 1
        logger.error("ofsted_provider_failed error_type=%s", type(exc).__name__)
        raise
    finally:
        if register_path is not None:
            register_path.unlink(missing_ok=True)
        logger.info(
            "ofsted_collection_summary fetched=%d matched=%d queued=%d duplicates=%d "
            "excluded=%d errors=%d registered_since_days=%d max_records=%d source=%s",
            counts["records_fetched"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["excluded"],
            counts["errors"],
            query.registered_since_days,
            query.max_records,
            source,
        )
        finish_run(
            settings,
            source_key="ofsted",
            run_id=run_id,
            started_at=started_at,
            status=status,
            counts=counts,
            failure_category=failure_category,
            failure_message=failure_message,
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_ofsted(Settings.from_env(), collector_payload(event))
