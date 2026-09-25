from __future__ import annotations

import json
from typing import Any

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

logger = configure_logging()


def collect_recruitment(
    settings: Settings,
    event: dict[str, Any] | None = None,
    provider: RecruitmentProvider | None = None,
) -> dict[str, int]:
    if provider is None:
        if not settings.recruitment_provider_secret_arn:
            raise RuntimeError("RECRUITMENT_PROVIDER_SECRET_ARN is not configured")
        api_key = provider_api_key_from_secret(settings.recruitment_provider_secret_arn)
        provider = GovApprenticeshipProvider(
            api_key, base_url=settings.recruitment_provider_base_url
        )
    if not settings.ingestion_queue_url:
        raise RuntimeError("INGESTION_QUEUE_URL is not configured")
    event = event or {}
    query = RecruitmentQuery.from_event(event)
    counts = {
        "records_fetched": 0,
        "candidates_matched": 0,
        "signals_queued": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": 0,
    }
    try:
        for record in provider.vacancies(query):
            counts["records_fetched"] += 1
            decision = classify_recruitment(record)
            if not decision["matched"]:
                counts["excluded"] += 1
                continue
            counts["candidates_matched"] += 1
            try:
                signal = recruitment_signal(record, decision)
                body = json.dumps(
                    {"message_version": "1.0", "signal": signal, "raw_provider_record": record.raw},
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
                    "recruitment_record_queue_failed external_id=%s", record.external_id
                )
    except Exception as exc:
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
            "provider=govuk-apprenticeships fetched=%d matched=%d queued=%d "
            "duplicates=%d excluded=%d errors=%d posted_since_days=%d "
            "max_records=%d page_size=%d source=%s",
            counts["records_fetched"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["duplicates"],
            counts["excluded"],
            counts["errors"],
            query.posted_since_days,
            query.max_records,
            query.page_size,
            event.get("source", "manual")[:32],
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_recruitment(Settings.from_env(), event)
