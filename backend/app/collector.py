from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import boto3

from app.care import care_planning_signal, classify_care_planning
from app.collector_events import collector_payload
from app.config import Settings
from app.logging import configure_logging
from app.planning import (
    CandidateDecision,
    PlanningProvider,
    PlanningQuery,
    PlanningRateLimitError,
    PlotaProvider,
    candidate_decision,
    planning_signal,
)
from app.planning_backfill import add_counts, bounds_from_chunk, chunk_payload
from app.queueing import (
    PlanningOriginRecoveryResultMessage,
    SignalIngestionMessage,
    send_ingestion_message,
    send_planning_origin_recovery_result,
)
from app.secrets import provider_api_key_from_secret
from app.source_runs import (
    finish_run,
    get_run,
    safe_failure,
    start_run,
    update_run_progress,
)
from app.verticals import CHILDRENS_HOME, NURSERY, VERTICAL_REGISTRY, validate_vertical

logger = configure_logging()


def recover_planning_origin(settings: Settings, payload: dict[str, Any]) -> dict[str, int]:
    """Run one exact-reference Plota lookup and queue a normal ingestion message."""
    attempt_id = str(payload["attempt_id"])
    reference = str(payload["normalized_reference"])
    authority = str(payload["planning_authority"])

    def report(status: str, details: dict[str, Any]) -> None:
        send_planning_origin_recovery_result(
            settings,
            PlanningOriginRecoveryResultMessage(
                message_version="1.0",
                message_type="planning_origin_recovery_result",
                attempt_id=attempt_id,
                status=status,
                details=details,
            ),
        )

    try:
        api_key = provider_api_key_from_secret(settings.planning_provider_secret_arn)
        provider = PlotaProvider(api_key, base_url=settings.planning_provider_base_url)
        matches = provider.applications_by_reference(reference, council=authority, limit=10)
        if not matches:
            status = "NOT_FOUND"
            details = {"requests": 1, "records_returned": 0}
            result = {"requests": 1, "records_fetched": 0, "signals_queued": 0}
        elif len(matches) > 1:
            status = "AMBIGUOUS"
            details = {"requests": 1, "records_returned": len(matches)}
            result = {
                "requests": 1,
                "records_fetched": len(matches),
                "signals_queued": 0,
            }
        else:
            record = matches[0]
            decision = classify_care_planning(record)
            if not decision.matched:
                status = "NOT_FOUND"
                details = {
                    "requests": 1,
                    "records_returned": 1,
                    "candidate_excluded": True,
                }
                result = {"requests": 1, "records_fetched": 1, "signals_queued": 0}
            else:
                signal = care_planning_signal(record, decision, historical_source_date=True)
                body = json.dumps(
                    {
                        "message_version": "1.0",
                        "signal": signal,
                        "raw_provider_record": record.raw,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
                send_ingestion_message(settings, SignalIngestionMessage.from_json(body))
                status = "FOUND"
                details = {
                    "requests": 1,
                    "records_returned": 1,
                    "external_id": signal["external_id"],
                }
                result = {"requests": 1, "records_fetched": 1, "signals_queued": 1}
    except Exception as exc:
        logger.exception("planning_origin_recovery_failed attempt_id=%s", attempt_id)
        status = "PROVIDER_ERROR"
        details = {"error_type": type(exc).__name__}
        result = {"requests": 1, "records_fetched": 0, "signals_queued": 0}
    # Keep callback delivery outside the provider/ingestion try block: if SQS
    # delivery fails, the manual-run message must retry instead of being
    # incorrectly converted into a provider error.
    report(status, details)
    return result


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
    historical_backfill = event.get("invocation_source") == "historical_backfill"
    query = (
        PlanningQuery.from_historical_event(event)
        if historical_backfill
        else PlanningQuery.from_event(event)
    )
    requested_verticals = _requested_verticals(event)
    source = str(event.get("source", "manual"))[:32]
    lookback_days = (
        (query.to_date - query.from_date).days + 1
        if historical_backfill
        else min(max(int(event.get("lookback_days", 2)), 1), 31)
    )
    care_max_records = min(max(int(event.get("care_max_records", 50)), 1), 100)
    run_id, started_at = start_run(
        settings,
        source_key="planning",
        provider="Plota",
        invocation_source=(
            "historical_backfill"
            if historical_backfill
            else "scheduled"
            if source == "scheduled"
            else "manual"
        ),
        parameters={
            "lookback_days": lookback_days,
            "from_date": query.from_date.isoformat(),
            "to_date": query.to_date.isoformat(),
            "max_records": query.max_records,
            "page_size": query.page_size,
            "care_max_records": care_max_records,
            **(
                {
                    "backfill_version": event.get("backfill_version"),
                    "backfill_id": event.get("backfill_id"),
                    "chunk_index": event.get("chunk_index"),
                    "chunks_total": event.get("chunks_total"),
                    "requested_from_date": event.get("requested_from_date"),
                    "requested_to_date": event.get("requested_to_date"),
                }
                if historical_backfill
                else {}
            ),
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
                    if historical_backfill:
                        counts["duplicates"] += 1
                    continue
                seen_records.add(record_key)
                counts["records_fetched"] += 1
                nursery_decision: CandidateDecision = candidate_decision(record)
                care_decision = classify_care_planning(record)
                signals = []
                if NURSERY in requested_verticals and nursery_decision.matched:
                    signals.append(
                        planning_signal(
                            record,
                            nursery_decision,
                            historical_source_date=historical_backfill,
                        )
                    )
                    counts["nursery_matched"] += 1
                if CHILDRENS_HOME in requested_verticals and care_decision.matched:
                    signals.append(
                        care_planning_signal(
                            record,
                            care_decision,
                            historical_source_date=historical_backfill,
                        )
                    )
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
    settings = Settings.from_env()
    payload = collector_payload(event)
    if payload.get("invocation_source") == "planning_origin_recovery":
        return recover_planning_origin(settings, payload)
    if payload.get("invocation_source") != "historical_backfill":
        return collect_planning(settings, payload)

    bounds = bounds_from_chunk(payload)
    backfill_id = str(payload["backfill_id"])
    parent_started_at = str(payload["parent_started_at"])
    chunk_index = int(payload["chunk_index"])
    if (
        payload.get("from_date") != bounds.chunks[chunk_index][0].isoformat()
        or payload.get("to_date") != bounds.chunks[chunk_index][1].isoformat()
    ):
        raise ValueError("historical Planning chunk boundaries are invalid")

    run_id = str(payload["run_id"])
    existing = get_run(
        settings, "planning", run_id=run_id, started_at=str(payload["run_started_at"])
    )
    try:
        counts = (
            existing.get("counts", {})
            if existing and existing.get("status") == "SUCCESS"
            else collect_planning(settings, payload)
        )
        cumulative = add_counts(payload.get("cumulative_counts") or {}, counts)
        cumulative["chunks_completed"] = chunk_index + 1
        cumulative["chunks_total"] = len(bounds.chunks)
        update_run_progress(
            settings,
            source_key="planning_backfill",
            run_id=backfill_id,
            started_at=parent_started_at,
            counts=cumulative,
        )
        next_index = chunk_index + 1
        if next_index < len(bounds.chunks):
            if not settings.planning_manual_run_queue_url:
                raise RuntimeError("PLANNING_MANUAL_RUN_QUEUE_URL is not configured")
            response = boto3.client("sqs").send_message(
                QueueUrl=settings.planning_manual_run_queue_url,
                DelaySeconds=60,
                MessageBody=json.dumps(
                    chunk_payload(
                        bounds,
                        backfill_id=backfill_id,
                        parent_started_at=parent_started_at,
                        chunk_index=next_index,
                        cumulative_counts=cumulative,
                    ),
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            if not response.get("MessageId"):
                raise RuntimeError("next historical Planning chunk was not accepted")
        else:
            finish_run(
                settings,
                source_key="planning_backfill",
                run_id=backfill_id,
                started_at=parent_started_at,
                status="SUCCESS",
                counts=cumulative,
            )
        return cumulative
    except Exception as exc:
        category, message = safe_failure(exc)
        finish_run(
            settings,
            source_key="planning_backfill",
            run_id=backfill_id,
            started_at=parent_started_at,
            status="FAILED",
            counts=payload.get("cumulative_counts") or {},
            failure_category=category,
            failure_message=message,
        )
        # PlotaProvider has already exhausted its bounded retry/backoff budget.
        # A further SQS redrive would only consume more source quota; retain the
        # failed run for an explicit later admin retry instead of poisoning the
        # shared collector DLQ.
        if isinstance(exc, PlanningRateLimitError):
            return payload.get("cumulative_counts") or {"errors": 1}
        raise
