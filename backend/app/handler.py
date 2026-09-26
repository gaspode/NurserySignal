from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3

from app.authorization import normalized_groups
from app.config import Settings
from app.db import check_connection
from app.ingestion import NormalizedSignal
from app.logging import configure_logging
from app.repository import (
    create_opportunity_from_signal,
    link_signal_to_opportunity,
    list_match_reviews,
    list_opportunities,
    list_signals,
    merge_opportunities,
    opportunity_detail,
    recalculate_opportunity_creation,
    record_admin_audit,
    reprocess_planning_signals,
    reprocess_recruitment_signals,
    resolve_match_review,
    review_signal,
    review_signals_bulk,
    signal_detail,
    split_opportunity,
    unlink_signal_from_opportunity,
)
from app.service import EnrichmentQueueError, SignalConflictError, ingest_signal, parse_json_payload
from app.shadow_review import reevaluate_ai_shadow
from app.source_runs import finish_run, list_runs, start_run
from app.storage import EvidencePersistenceError, presigned_evidence_url

logger = configure_logging()


SOURCE_DEFINITIONS = {
    "planning": {
        "display_name": "Planning applications",
        "provider": "Plota",
        "schedule_state": "ENABLED",
        "schedule_expression": "rate(1 day)",
        "default_parameters": {
            "source": "manual",
            "lookback_days": 2,
            "max_records": 100,
            "page_size": 25,
        },
        "function_setting": "planning_collector_function_name",
    },
    "recruitment": {
        "display_name": "Recruitment vacancies",
        "provider": "GOV.UK Apprenticeships",
        "schedule_state": "ENABLED",
        "schedule_expression": "rate(1 day)",
        "default_parameters": {
            "source": "manual",
            "posted_since_days": 7,
            "max_records": 50,
            "page_size": 25,
        },
        "function_setting": "recruitment_collector_function_name",
    },
}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date, UUID)):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, separators=(",", ":"), default=_json_default),
    }


def _claims(event: dict[str, Any]) -> dict[str, Any] | None:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    return claims if isinstance(claims, dict) and claims else None


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return str(body).encode("utf-8")


def _require_claims(event: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    claims = _claims(event)
    if claims is None:
        return None, _response(401, {"error": "authentication_required"})
    return claims, None


def _require_admin(claims: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    if settings.admin_group not in normalized_groups(claims.get("cognito:groups")):
        return _response(403, {"error": "administrator_role_required"})
    return None


def _signal_payload(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = _raw_body(event)
    if len(raw_body) > 256 * 1024:
        raise ValueError("request body exceeds 256 KiB")
    payload = parse_json_payload(raw_body)
    signal = NormalizedSignal.from_dict(payload)
    if len(signal.raw_text) > 100_000:
        raise ValueError("raw_text exceeds 100000 characters")
    if len(signal.metadata) > 100:
        raise ValueError("metadata contains too many fields")
    return {"signal": signal, "raw_body": raw_body}


def _query(event: dict[str, Any], name: str) -> str | None:
    value = event.get("queryStringParameters", {}).get(name)
    return str(value) if value not in (None, "") else None


def _admin_path(path: str) -> tuple[str, str | None]:
    if path == "/admin/sources":
        return "source-list", None
    if path in {"/admin/sources/planning/run", "/admin/sources/recruitment/run"}:
        return "source-run", path.split("/")[3]
    if path == "/admin/planning/reprocess":
        return "reprocess", None
    if path == "/admin/recruitment/reprocess":
        return "recruitment-reprocess", None
    if path == "/admin/opportunities":
        return "opportunity-list", None
    if path == "/admin/opportunities/recalculate":
        return "opportunity-recalculate", None
    if path.startswith("/admin/opportunities/"):
        parts = path[len("/admin/opportunities/") :].split("/")
        if len(parts) == 2 and parts[1] in {"link", "unlink", "merge", "split"}:
            return f"opportunity-{parts[1]}", parts[0]
        return "opportunity-detail", parts[0]
    if path == "/admin/match-review":
        return "match-review-list", None
    if path.startswith("/admin/match-review/"):
        parts = path[len("/admin/match-review/") :].split("/")
        if len(parts) == 2 and parts[1] in {"link", "reject"}:
            return f"match-review-{parts[1]}", parts[0]
    if path == "/admin/signals/bulk-review":
        return "bulk-review", None
    prefix = "/admin/signals"
    if path == prefix:
        return "list", None
    if path.startswith(prefix + "/"):
        remainder = path[len(prefix) + 1 :]
        parts = remainder.split("/")
        if len(parts) == 1:
            return "detail", parts[0]
        if len(parts) == 2 and parts[1] in {
            "approve",
            "reject",
            "evidence",
            "ai-review",
            "create-opportunity",
        }:
            return parts[1], parts[0]
    return "unknown", None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    path = event.get("rawPath") or event.get("path") or "/"
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    logger.info("request path=%s method=%s environment=%s", path, method, settings.environment)

    if method == "OPTIONS":
        return _response(204, {})

    if path == "/health" and method == "GET":
        db_configured = settings.database_url is not None or settings.db_secret_arn is not None
        db_ok = check_connection(settings) if db_configured else False
        status = "ok" if db_ok else "degraded"
        return _response(
            200 if db_ok else 503,
            {
                "status": status,
                "service": settings.service_name,
                "database": "connected" if db_ok else "unavailable",
            },
        )

    if path == "/" and method == "GET":
        return _response(200, {"service": settings.service_name, "status": "ready"})

    if path == "/signals" and method == "POST":
        _, auth_error = _require_claims(event)
        if auth_error:
            return auth_error
        try:
            values = _signal_payload(event)
            result = ingest_signal(settings, values["signal"], values["raw_body"])
            return _response(
                200 if result.status == "duplicate" else 201,
                {
                    "signal_id": result.signal_id,
                    "status": result.status,
                    "enrichment_queued": result.enrichment_queued,
                    "evidence_key": result.evidence_key,
                },
            )
        except SignalConflictError as exc:
            return _response(409, {"error": str(exc)})
        except ValueError as exc:
            return _response(400, {"error": str(exc)})
        except EvidencePersistenceError:
            logger.exception("ingestion_evidence_failed")
            return _response(503, {"error": "evidence_persistence_failed"})
        except EnrichmentQueueError:
            logger.exception("ingestion_queue_failed")
            return _response(503, {"error": "enrichment_queue_failed"})
        except Exception:
            logger.exception("ingestion_failed")
            return _response(500, {"error": "ingestion_failed"})

    if path.startswith("/admin/") or path == "/admin/signals":
        claims, auth_error = _require_claims(event)
        if auth_error:
            return auth_error
        action, signal_id = _admin_path(path)
        try:
            if action == "source-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                sources = []
                for source_key, definition in SOURCE_DEFINITIONS.items():
                    runs = list_runs(settings, source_key, limit=10)
                    latest = runs[0] if runs else None
                    successful = next((run for run in runs if run.get("status") == "SUCCESS"), None)
                    sources.append(
                        {
                            "key": source_key,
                            "display_name": definition["display_name"],
                            "provider": definition["provider"],
                            "schedule_state": definition["schedule_state"],
                            "schedule_expression": definition["schedule_expression"],
                            "last_attempt_at": latest.get("started_at") if latest else None,
                            "last_success_at": (
                                successful.get("completed_at") if successful else None
                            ),
                            "last_status": latest.get("status") if latest else None,
                            "last_run": latest,
                            "last_summary": latest.get("counts", {}) if latest else {},
                            "last_error": (
                                {
                                    "category": latest.get("failure_category"),
                                    "message": latest.get("failure_message"),
                                }
                                if latest and latest.get("failure_category")
                                else None
                            ),
                            "manual_run_allowed": True,
                            "default_parameters": definition["default_parameters"],
                            "recent_runs": runs,
                        }
                    )
                return _response(200, {"items": sources})
            if action == "source-run" and method == "POST" and signal_id in SOURCE_DEFINITIONS:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                definition = SOURCE_DEFINITIONS[signal_id]
                function_name = getattr(settings, definition["function_setting"])
                if not function_name:
                    return _response(503, {"error": "collector_not_configured"})
                parameters = dict(definition["default_parameters"])
                run_id, started_at = start_run(
                    settings,
                    source_key=signal_id,
                    provider=definition["provider"],
                    invocation_source="manual",
                    parameters=parameters,
                )
                payload = {**parameters, "run_id": run_id, "run_started_at": started_at}
                try:
                    response = boto3.client("lambda").invoke(
                        FunctionName=function_name,
                        InvocationType="Event",
                        Payload=json.dumps(payload, separators=(",", ":")).encode(),
                    )
                    if response.get("StatusCode") not in {200, 202}:
                        raise RuntimeError("collector invocation was not accepted")
                except Exception as exc:
                    category, message = type(exc).__name__, str(exc)[:240].replace("\n", " ")
                    finish_run(
                        settings,
                        source_key=signal_id,
                        run_id=run_id,
                        started_at=started_at,
                        status="FAILED",
                        counts={},
                        failure_category=category,
                        failure_message=message,
                    )
                    logger.error(
                        "source_manual_run_invoke_failed source=%s error_type=%s",
                        signal_id,
                        category,
                    )
                    return _response(503, {"error": "collector_invocation_failed"})
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                try:
                    record_admin_audit(
                        settings,
                        action="source_manual_run",
                        actor=actor,
                        target_type="collector",
                        details={
                            "source_key": signal_id,
                            "run_id": run_id,
                            "parameters": parameters,
                        },
                    )
                except Exception:
                    logger.exception(
                        "source_manual_run_audit_failed source=%s run_id=%s",
                        signal_id,
                        run_id,
                    )
                return _response(
                    202, {"source_key": signal_id, "run_id": run_id, "status": "RUNNING"}
                )
            if action == "reprocess" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                limit = min(max(int(payload.get("limit", 25)), 1), 100)
                discovered_from = payload.get("discovered_from")
                discovered_to = payload.get("discovered_to")
                for value in (discovered_from, discovered_to):
                    if value is not None:
                        date.fromisoformat(str(value))
                signal_ids = payload.get("signal_ids")
                if signal_ids is not None:
                    if (
                        not isinstance(signal_ids, list)
                        or not signal_ids
                        or len(signal_ids) > limit
                    ):
                        raise ValueError("signal_ids must be a non-empty list within the limit")
                    signal_ids = [str(UUID(str(value))) for value in signal_ids]
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                result = reprocess_planning_signals(
                    settings,
                    actor=actor,
                    limit=limit,
                    discovered_from=str(discovered_from) if discovered_from else None,
                    discovered_to=str(discovered_to) if discovered_to else None,
                    signal_ids=signal_ids,
                )
                return _response(
                    200,
                    {
                        "operation_id": result.operation_id,
                        "selected": result.selected,
                        "pending_updated": result.pending_updated,
                        "reviewed_preserved": result.reviewed_preserved,
                        "without_enrichment": result.without_enrichment,
                        "matched": result.matched,
                        "excluded": result.excluded,
                    },
                )
            if action == "recruitment-reprocess" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                limit = min(max(int(payload.get("limit", 25)), 1), 100)
                signal_ids = payload.get("signal_ids")
                if signal_ids is not None:
                    if (
                        not isinstance(signal_ids, list)
                        or not signal_ids
                        or len(signal_ids) > limit
                    ):
                        raise ValueError("signal_ids must be a non-empty list within the limit")
                    signal_ids = [str(UUID(str(value))) for value in signal_ids]
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    reprocess_recruitment_signals(
                        settings, actor=actor, limit=limit, signal_ids=signal_ids
                    ),
                )
            if action == "ai-review" and method == "POST" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                result = reevaluate_ai_shadow(settings, signal_id)
                return _response(200, result) if result else _response(404, {"error": "not_found"})
            if action == "bulk-review" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                action_name = payload.get("action")
                if action_name not in {"approve", "reject"}:
                    raise ValueError("action must be approve or reject")
                values = payload.get("signal_ids")
                if not isinstance(values, list) or not 1 <= len(values) <= 100:
                    raise ValueError("signal_ids must contain between 1 and 100 IDs")
                signal_ids = [str(UUID(str(value))) for value in values]
                reviewer = str(claims.get("sub") or claims.get("username") or "unknown")
                status = "APPROVED" if action_name == "approve" else "REJECTED"
                return _response(
                    200,
                    review_signals_bulk(settings, signal_ids, status, reviewer),
                )
            if action == "match-review-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                limit = min(max(int(_query(event, "limit") or "25"), 1), 100)
                offset = max(int(_query(event, "offset") or "0"), 0)
                return _response(200, list_match_reviews(settings, limit=limit, offset=offset))
            if (
                action in {"match-review-link", "match-review-reject"}
                and method == "POST"
                and signal_id
            ):
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    resolve_match_review(
                        settings,
                        signal_id,
                        "link" if action == "match-review-link" else "reject",
                        actor,
                    ),
                )
            if (
                action
                in {
                    "opportunity-link",
                    "opportunity-unlink",
                    "opportunity-merge",
                    "opportunity-split",
                }
                and method == "POST"
                and signal_id
            ):
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                if action == "opportunity-link":
                    target_signal = str(payload.get("signal_id") or "")
                    if not target_signal:
                        raise ValueError("signal_id_required")
                    return _response(
                        200,
                        link_signal_to_opportunity(
                            settings,
                            signal_id,
                            target_signal,
                            actor,
                            str(payload.get("reason") or "admin link"),
                        ),
                    )
                if action == "opportunity-unlink":
                    target_signal = str(payload.get("signal_id") or "")
                    if not target_signal:
                        raise ValueError("signal_id_required")
                    if not unlink_signal_from_opportunity(
                        settings,
                        signal_id,
                        target_signal,
                        actor,
                        str(payload.get("reason") or "admin unlink"),
                    ):
                        return _response(404, {"error": "relationship_not_found"})
                    return _response(
                        200,
                        {
                            "status": "REJECTED",
                            "signal_id": target_signal,
                            "opportunity_id": signal_id,
                        },
                    )
                if action == "opportunity-merge":
                    target = str(payload.get("target_opportunity_id") or "")
                    if not target:
                        raise ValueError("target_opportunity_id_required")
                    return _response(200, merge_opportunities(settings, signal_id, target, actor))
                values = payload.get("signal_ids")
                if not isinstance(values, list) or not 1 <= len(values) <= 100:
                    raise ValueError("signal_ids must contain between 1 and 100 IDs")
                return _response(
                    200,
                    split_opportunity(
                        settings,
                        signal_id,
                        [str(UUID(v)) for v in values],
                        actor,
                        payload.get("name"),
                    ),
                )
            if action == "create-opportunity" and method == "POST" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(200, create_opportunity_from_signal(settings, signal_id, actor))
            if action == "list" and method == "GET":
                limit = min(max(int(_query(event, "limit") or "25"), 1), 100)
                offset = max(int(_query(event, "offset") or "0"), 0)
                return _response(
                    200,
                    list_signals(
                        settings,
                        limit=limit,
                        offset=offset,
                        review_status=_query(event, "review_status"),
                        source_type=_query(event, "source_type"),
                        discovered_from=_query(event, "discovered_from"),
                        discovered_to=_query(event, "discovered_to"),
                        search=_query(event, "q"),
                        unmatched_only=_query(event, "unmatched") == "true",
                        include_excluded=_query(event, "include_excluded") == "true",
                        opportunity_decision=_query(event, "opportunity_decision"),
                    ),
                )
            if action == "opportunity-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                limit = min(max(int(_query(event, "limit") or "25"), 1), 100)
                offset = max(int(_query(event, "offset") or "0"), 0)
                return _response(
                    200,
                    list_opportunities(
                        settings, limit=limit, offset=offset, search=_query(event, "q")
                    ),
                )
            if action == "opportunity-recalculate" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                limit = min(max(int(payload.get("limit", 50)), 1), 100)
                signal_ids = payload.get("signal_ids")
                if signal_ids is not None:
                    if (
                        not isinstance(signal_ids, list)
                        or not signal_ids
                        or len(signal_ids) > limit
                    ):
                        raise ValueError("signal_ids must be a non-empty list within the limit")
                    signal_ids = [str(UUID(str(value))) for value in signal_ids]
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    recalculate_opportunity_creation(
                        settings, actor=actor, limit=limit, signal_ids=signal_ids
                    ),
                )
            if action == "opportunity-detail" and method == "GET" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                detail = opportunity_detail(settings, signal_id)
                return _response(200, detail) if detail else _response(404, {"error": "not_found"})
            if action == "detail" and method == "GET" and signal_id:
                detail = signal_detail(settings, signal_id)
                return _response(200, detail) if detail else _response(404, {"error": "not_found"})
            if action == "evidence" and method == "GET" and signal_id:
                detail = signal_detail(settings, signal_id)
                if detail is None:
                    return _response(404, {"error": "not_found"})
                documents = detail.get("documents") or []
                if not documents:
                    return _response(404, {"error": "evidence_not_found"})
                document = documents[0]
                return _response(
                    200,
                    {
                        "url": presigned_evidence_url(document["s3_bucket"], document["s3_key"]),
                        "expires_in": 300,
                    },
                )
            if action in {"approve", "reject"} and method == "POST" and signal_id:
                status = "APPROVED" if action == "approve" else "REJECTED"
                reviewer = str(claims.get("sub") or claims.get("username") or "unknown")
                if not review_signal(settings, signal_id, status, reviewer):
                    return _response(404, {"error": "enrichment_not_found"})
                return _response(200, {"signal_id": signal_id, "review_status": status})
        except (ValueError, TypeError):
            return _response(400, {"error": "invalid_admin_request"})
        except Exception:
            logger.exception("admin_request_failed action=%s signal_id=%s", action, signal_id)
            return _response(500, {"error": "admin_request_failed"})
        return _response(404, {"error": "not_found"})

    return _response(404, {"error": "not_found"})
