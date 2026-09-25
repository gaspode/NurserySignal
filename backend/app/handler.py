from __future__ import annotations

import base64
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.authorization import normalized_groups
from app.config import Settings
from app.db import check_connection
from app.ingestion import NormalizedSignal
from app.logging import configure_logging
from app.repository import (
    list_opportunities,
    list_signals,
    opportunity_detail,
    reprocess_planning_signals,
    reprocess_recruitment_signals,
    review_signal,
    signal_detail,
)
from app.service import EnrichmentQueueError, SignalConflictError, ingest_signal, parse_json_payload
from app.shadow_review import reevaluate_ai_shadow
from app.storage import EvidencePersistenceError, presigned_evidence_url

logger = configure_logging()


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
    if path == "/admin/planning/reprocess":
        return "reprocess", None
    if path == "/admin/recruitment/reprocess":
        return "recruitment-reprocess", None
    if path == "/admin/opportunities":
        return "opportunity-list", None
    if path.startswith("/admin/opportunities/"):
        return "opportunity-detail", path[len("/admin/opportunities/") :]
    prefix = "/admin/signals"
    if path == prefix:
        return "list", None
    if path.startswith(prefix + "/"):
        remainder = path[len(prefix) + 1 :]
        parts = remainder.split("/")
        if len(parts) == 1:
            return "detail", parts[0]
        if len(parts) == 2 and parts[1] in {"approve", "reject", "evidence", "ai-review"}:
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
                    ),
                )
            if action == "opportunity-list" and method == "GET":
                limit = min(max(int(_query(event, "limit") or "25"), 1), 100)
                offset = max(int(_query(event, "offset") or "0"), 0)
                return _response(
                    200,
                    list_opportunities(
                        settings, limit=limit, offset=offset, search=_query(event, "q")
                    ),
                )
            if action == "opportunity-detail" and method == "GET" and signal_id:
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
