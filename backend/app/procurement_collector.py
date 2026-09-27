from __future__ import annotations

import json
from typing import Any

from app.collector_events import collector_payload
from app.config import Settings
from app.logging import configure_logging
from app.procurement import (
    CONTRACTS_FINDER,
    FIND_A_TENDER,
    ProcurementQuery,
    iter_evaluation_records,
    iter_releases,
    procurement_signal,
)
from app.queueing import SignalIngestionMessage, send_ingestion_message
from app.source_runs import finish_run, safe_failure, start_run

logger = configure_logging()


def collect_procurement(settings: Settings, event: dict[str, Any] | None = None) -> dict[str, int]:
    event = event or {}
    query = ProcurementQuery.from_event(event)
    invocation_source = str(event.get("source") or "manual")[:32]
    run_id, started_at = start_run(
        settings,
        source_key="procurement",
        provider="Find a Tender + Contracts Finder",
        invocation_source=invocation_source,
        parameters={
            "published_since_days": query.published_since_days,
            "max_records_per_source": query.max_records_per_source,
            "page_size": query.page_size,
            "mode": "SHADOW_ONLY",
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
        "find_a_tender_fetched": 0,
        "contracts_finder_fetched": 0,
        "NEW_HOME_COMMISSIONING": 0,
        "NEW_CAPACITY_MARKET_ENGAGEMENT": 0,
        "OPERATOR_PROCUREMENT": 0,
        "CONTRACT_AWARD": 0,
        "ROUTINE_PLACEMENT_FRAMEWORK": 0,
        "EXISTING_SERVICE_REPROCUREMENT": 0,
        "UNCERTAIN": 0,
        "IRRELEVANT": 0,
        "latest_publication_seen": None,
    }
    status = "SUCCESS"
    failure_category = failure_message = None
    try:
        if not settings.ingestion_queue_url:
            raise RuntimeError("INGESTION_QUEUE_URL is not configured")
        seen_release_ids: set[tuple[str, str]] = set()

        def process(
            platform: str,
            release: dict[str, Any],
            research_key: str | None = None,
        ) -> None:
            identity = (platform, str(release.get("id") or release.get("ocid") or ""))
            if identity in seen_release_ids:
                counts["duplicates"] += 1
                return
            seen_release_ids.add(identity)
            counts["records_fetched"] += 1
            counts[f"{platform}_fetched"] += 1
            signal, safe_release = procurement_signal(platform, release)
            if research_key:
                signal["metadata"]["evaluation_research_key"] = research_key
            publication = str(signal["metadata"].get("publication_date") or "")
            if publication and publication > str(counts["latest_publication_seen"] or ""):
                counts["latest_publication_seen"] = publication
            category = str(signal["metadata"]["procurement_category"])
            counts[category] += 1
            if category == "IRRELEVANT":
                counts["excluded"] += 1
                return
            body = json.dumps(
                {
                    "message_version": "1.0",
                    "signal": signal,
                    "raw_provider_record": {
                        "platform": platform,
                        "evaluation_research_key": research_key,
                        "release": safe_release,
                        "classification_at_collection": {
                            "category": category,
                            "confidence": signal["metadata"]["procurement_confidence"],
                            "reason": signal["metadata"]["procurement_reason"],
                        },
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            if len(body) > 240_000:
                counts["errors"] += 1
                logger.warning(
                    "procurement_release_too_large platform=%s release_id=%s",
                    platform,
                    str(release.get("id") or "unknown")[:80],
                )
                return
            send_ingestion_message(settings, SignalIngestionMessage.from_json(body))
            counts["candidates_matched"] += 1
            counts["signals_queued"] += 1

        for platform in (FIND_A_TENDER, CONTRACTS_FINDER):
            for release in iter_releases(platform, query):
                process(platform, release)
        for research_key, platform, release in iter_evaluation_records():
            process(platform, release, research_key)
    except Exception as exc:
        status = "FAILED"
        failure_category, failure_message = safe_failure(exc)
        counts["errors"] += 1
        logger.error("procurement_provider_failed error_type=%s", type(exc).__name__)
        raise
    finally:
        logger.info(
            "procurement_collection_summary fetched=%d matched=%d queued=%d excluded=%d "
            "errors=%d find_a_tender=%d contracts_finder=%d strong=%d routine=%d "
            "uncertain=%d lookback_days=%d max_records_per_source=%d page_size=%d "
            "source=%s mode=SHADOW_ONLY",
            counts["records_fetched"],
            counts["candidates_matched"],
            counts["signals_queued"],
            counts["excluded"],
            counts["errors"],
            counts["find_a_tender_fetched"],
            counts["contracts_finder_fetched"],
            sum(
                counts[key]
                for key in (
                    "NEW_HOME_COMMISSIONING",
                    "NEW_CAPACITY_MARKET_ENGAGEMENT",
                    "OPERATOR_PROCUREMENT",
                    "CONTRACT_AWARD",
                )
            ),
            counts["ROUTINE_PLACEMENT_FRAMEWORK"]
            + counts["EXISTING_SERVICE_REPROCUREMENT"],
            counts["UNCERTAIN"],
            query.published_since_days,
            query.max_records_per_source,
            query.page_size,
            invocation_source,
        )
        finish_run(
            settings,
            source_key="procurement",
            run_id=run_id,
            started_at=started_at,
            status=status,
            counts=counts,
            failure_category=failure_category,
            failure_message=failure_message,
        )
    return counts


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    return collect_procurement(Settings.from_env(), collector_payload(event))
