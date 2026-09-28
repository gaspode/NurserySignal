from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3

from app.authorization import normalized_groups
from app.backtest_repository import (
    CARE_BENCHMARK_VERSION,
    backtesting_summary,
    compare_backtest_runs,
    evaluate_current_care_recruitment,
    execute_backtest,
    execute_backtest_sensitivity,
    get_backtest_run,
    labelled_decisions,
    seed_care_ofsted_benchmark,
)
from app.backtesting import BacktestBounds
from app.care_backfill import backfill_care_from_stored_evidence
from app.config import Settings
from app.customer import (
    apply_pilot_publications,
    create_saved_search,
    customer_context,
    customer_opportunity_detail,
    customer_readiness,
    digest_preview,
    get_customer_preferences,
    list_customer_accounts,
    list_customer_opportunities,
    list_saved_searches,
    pilot_curation_inventory,
    provision_customer_account,
    record_customer_event,
    save_customer_opportunity,
    set_opportunity_publication,
    update_customer_account,
    update_customer_preferences,
)
from app.customer_digest import queue_weekly_digests, update_digest_delivery
from app.db import check_connection
from app.historical_research_repository import import_bundled_historical_corpus
from app.ingestion import NormalizedSignal
from app.logging import configure_logging
from app.repository import (
    create_opportunity_from_signal,
    link_signal_to_opportunity,
    list_match_reviews,
    list_opportunities,
    list_organisation_enrichment_candidates,
    list_organisation_match_reviews,
    list_organisations,
    list_procurement_evaluations,
    list_signals,
    merge_opportunities,
    opportunity_detail,
    organisation_detail,
    recalculate_opportunity_creation,
    record_admin_audit,
    reprocess_planning_signals,
    reprocess_recruitment_signals,
    resolve_match_review,
    resolve_organisation_match_review,
    review_signal,
    review_signals_bulk,
    signal_detail,
    split_opportunity,
    unlink_signal_from_opportunity,
)
from app.service import EnrichmentQueueError, SignalConflictError, ingest_signal, parse_json_payload
from app.shadow_review import UnsupportedShadowSourceError, reevaluate_ai_shadow
from app.source_runs import finish_run, list_runs, start_run
from app.storage import EvidencePersistenceError, presigned_evidence_url
from app.verticals import ALL_VERTICALS, registry_payload, validate_vertical_filter

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
            "care_max_records": 50,
            "page_size": 25,
        },
        "queue_setting": "planning_manual_run_queue_url",
        "supported_verticals": ["NURSERY", "CHILDRENS_HOME"],
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
            "care_max_records": 50,
            "page_size": 25,
        },
        "queue_setting": "recruitment_manual_run_queue_url",
        "supported_verticals": ["NURSERY", "CHILDRENS_HOME"],
    },
    "ofsted": {
        "display_name": "Ofsted children's social care register",
        "provider": "Ofsted",
        "schedule_state": "DISABLED",
        "schedule_expression": "Manual only",
        "default_parameters": {
            "source": "manual",
            "registered_since_days": 730,
            "max_records": 50,
            "active_only": True,
            "urn_enrichment_limit": 10,
        },
        "queue_setting": "ofsted_manual_run_queue_url",
        "supported_verticals": ["CHILDRENS_HOME"],
    },
    "companies_house": {
        "display_name": "Organisation enrichment",
        "provider": "Companies House",
        "schedule_state": "DISABLED",
        "schedule_expression": "Manual only",
        "default_parameters": {"source": "manual", "max_organisations": 10},
        "queue_setting": "companies_house_manual_run_queue_url",
        "supported_verticals": ["NURSERY", "CHILDRENS_HOME"],
    },
    "procurement": {
        "display_name": "Public procurement",
        "provider": "Find a Tender + Contracts Finder",
        "schedule_state": "DISABLED",
        "schedule_expression": "Manual shadow evaluation only",
        "default_parameters": {
            "source": "manual",
            "published_since_days": 548,
            "max_records_per_source": 300,
            "page_size": 100,
        },
        "queue_setting": "procurement_manual_run_queue_url",
        "supported_verticals": ["CHILDRENS_HOME"],
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


def _require_customer(
    claims: dict[str, Any], settings: Settings
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if settings.customer_group not in normalized_groups(claims.get("cognito:groups")):
        return None, _response(403, {"error": "customer_role_required"})
    context = customer_context(settings, claims)
    if context is None:
        return None, _response(403, {"error": "customer_account_required"})
    if context["user_status"] != "ACTIVE" or context["account_status"] == "SUSPENDED":
        return None, _response(403, {"error": "customer_account_suspended"})
    return context, None


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
    if path == "/admin/customer-accounts":
        return "customer-account-list", None
    if path == "/admin/customer-readiness":
        return "customer-readiness", None
    if path.startswith("/admin/customer-accounts/"):
        return "customer-account-detail", path[len("/admin/customer-accounts/") :]
    if path == "/admin/backtesting":
        return "backtesting-list", None
    if path == "/admin/backtesting/seed":
        return "backtesting-seed", None
    if path == "/admin/backtesting/research/import":
        return "backtesting-research-import", None
    if path == "/admin/backtesting/run":
        return "backtesting-run", None
    if path == "/admin/backtesting/sensitivity":
        return "backtesting-sensitivity", None
    if path == "/admin/backtesting/recruitment-shadow":
        return "backtesting-recruitment-shadow", None
    if path == "/admin/backtesting/compare":
        return "backtesting-compare", None
    if path == "/admin/backtesting/labels":
        return "backtesting-labels", None
    if path.startswith("/admin/backtesting/runs/"):
        return "backtesting-detail", path[len("/admin/backtesting/runs/") :]
    if path == "/admin/verticals":
        return "vertical-list", None
    if path == "/admin/organisations":
        return "organisation-list", None
    if path == "/admin/organisation-match-review":
        return "organisation-review-list", None
    if path.startswith("/admin/organisation-match-review/"):
        parts = path[len("/admin/organisation-match-review/") :].split("/")
        if len(parts) == 2 and parts[1] in {"confirm", "reject"}:
            return f"organisation-review-{parts[1]}", parts[0]
    if path.startswith("/admin/organisations/"):
        return "organisation-detail", path[len("/admin/organisations/") :]
    if path == "/admin/sources":
        return "source-list", None
    if path == "/admin/procurement-evaluation":
        return "procurement-evaluation", None
    if path.startswith("/admin/sources/") and path.endswith("/run"):
        parts = path.split("/")
        if len(parts) == 5 and parts[3] in SOURCE_DEFINITIONS:
            return "source-run", parts[3]
    if path == "/admin/planning/reprocess":
        return "reprocess", None
    if path == "/admin/recruitment/reprocess":
        return "recruitment-reprocess", None
    if path == "/admin/verticals/CHILDRENS_HOME/backfill":
        return "care-backfill", None
    if path == "/admin/opportunities":
        return "opportunity-list", None
    if path == "/admin/opportunities/recalculate":
        return "opportunity-recalculate", None
    if path.startswith("/admin/opportunities/"):
        parts = path[len("/admin/opportunities/") :].split("/")
        if len(parts) == 2 and parts[1] == "publication":
            return "opportunity-publication", parts[0]
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
    if event.get("operation") == "customer_pilot_inventory" and not event.get("requestContext"):
        return pilot_curation_inventory(settings, limit=int(event.get("limit") or 100))
    if event.get("operation") == "customer_pilot_publish" and not event.get("requestContext"):
        return apply_pilot_publications(
            settings,
            event.get("publications"),
            actor=str(event.get("actor") or "iam-operational-pilot-activation")[:200],
        )
    if event.get("operation") == "customer_weekly_digest" and not event.get("requestContext"):
        return queue_weekly_digests(settings)
    if event.get("operation") == "customer_digest_delivery" and not event.get("requestContext"):
        update_digest_delivery(
            settings,
            str(UUID(event["run_id"])),
            status=str(event.get("status") or ""),
            safe_failure=event.get("safe_failure"),
        )
        return {"status": "updated"}
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
        claims, auth_error = _require_claims(event)
        if auth_error:
            return auth_error
        admin_error = _require_admin(claims, settings)
        if admin_error:
            return admin_error
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

    if path == "/customer" or path.startswith("/customer/"):
        claims, auth_error = _require_claims(event)
        if auth_error:
            return auth_error
        try:
            customer, customer_error = _require_customer(claims, settings)
            if customer_error:
                return customer_error
            assert customer is not None
            if path == "/customer/me" and method == "GET":
                return _response(200, customer)
            if path in {"/customer/opportunities", "/customer/saved"} and method == "GET":
                return _response(
                    200,
                    list_customer_opportunities(
                        settings,
                        customer,
                        limit=min(max(int(_query(event, "limit") or "25"), 1), 50),
                        offset=max(int(_query(event, "offset") or "0"), 0),
                        search=_query(event, "q"),
                        region=_query(event, "region"),
                        local_authority=_query(event, "local_authority"),
                        change_type=_query(event, "change_type"),
                        stage=_query(event, "stage"),
                        source_type=_query(event, "source_type"),
                        first_detected_from=_query(event, "first_detected_from"),
                        updated_since=_query(event, "updated_since"),
                        saved_only=path == "/customer/saved",
                    ),
                )
            if path.startswith("/customer/opportunities/"):
                parts = path[len("/customer/opportunities/") :].split("/")
                opportunity_id = str(UUID(parts[0]))
                if len(parts) == 1 and method == "GET":
                    detail = customer_opportunity_detail(settings, customer, opportunity_id)
                    return (
                        _response(200, detail)
                        if detail
                        else _response(404, {"error": "opportunity_not_found"})
                    )
                if len(parts) == 2 and parts[1] == "save" and method in {"POST", "DELETE"}:
                    save_customer_opportunity(
                        settings, customer, opportunity_id, saved=method == "POST"
                    )
                    record_customer_event(
                        settings,
                        customer,
                        {
                            "event_type": (
                                "OPPORTUNITY_SAVED"
                                if method == "POST"
                                else "OPPORTUNITY_UNSAVED"
                            ),
                            "opportunity_id": opportunity_id,
                        },
                    )
                    return _response(200, {"id": opportunity_id, "saved": method == "POST"})
            if path == "/customer/preferences":
                if method == "GET":
                    return _response(200, get_customer_preferences(settings, customer))
                if method == "PUT":
                    return _response(
                        200,
                        update_customer_preferences(
                            settings, customer, parse_json_payload(_raw_body(event))
                        ),
                    )
            if path == "/customer/saved-searches":
                if method == "GET":
                    return _response(200, {"items": list_saved_searches(settings, customer)})
                if method == "POST":
                    return _response(
                        201,
                        create_saved_search(
                            settings, customer, parse_json_payload(_raw_body(event))
                        ),
                    )
            if path == "/customer/digest/preview" and method == "GET":
                return _response(200, digest_preview(settings, customer))
            if path == "/customer/events" and method == "POST":
                record_customer_event(
                    settings, customer, parse_json_payload(_raw_body(event))
                )
                return _response(202, {"status": "recorded"})
        except PermissionError as exc:
            return _response(403, {"error": str(exc)})
        except (ValueError, TypeError):
            return _response(400, {"error": "invalid_customer_request"})
        except Exception:
            logger.exception("customer_request_failed path=%s", path)
            return _response(500, {"error": "customer_request_failed"})
        return _response(404, {"error": "not_found"})

    if path.startswith("/admin/") or path == "/admin/signals":
        claims, auth_error = _require_claims(event)
        if auth_error:
            return auth_error
        admin_error = _require_admin(claims, settings)
        if admin_error:
            return admin_error
        action, signal_id = _admin_path(path)
        try:
            if action == "customer-account-list":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                if method == "GET":
                    return _response(200, {"items": list_customer_accounts(settings)})
                if method == "POST":
                    actor = str(claims.get("sub") or claims.get("username") or "unknown")
                    return _response(
                        201,
                        provision_customer_account(
                            settings, parse_json_payload(_raw_body(event)), actor
                        ),
                    )
            if action == "customer-account-detail" and method == "PATCH" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    update_customer_account(
                        settings,
                        str(UUID(signal_id)),
                        parse_json_payload(_raw_body(event)),
                        actor,
                    ),
                )
            if action == "customer-readiness" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                return _response(200, customer_readiness(settings))
            if action == "opportunity-publication" and method == "POST" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    set_opportunity_publication(
                        settings,
                        str(UUID(signal_id)),
                        parse_json_payload(_raw_body(event)),
                        actor,
                    ),
                )
            if action == "backtesting-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                return _response(
                    200,
                    backtesting_summary(
                        settings,
                        vertical=validate_vertical_filter(_query(event, "vertical")),
                        benchmark_version=_query(event, "benchmark_version"),
                    ),
                )
            if action == "backtesting-detail" and method == "GET" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                detail = get_backtest_run(settings, str(UUID(signal_id)))
                return _response(200, detail) if detail else _response(404, {"error": "not_found"})
            if action == "backtesting-seed" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                vertical = str(payload.get("vertical") or "CHILDRENS_HOME")
                if vertical != "CHILDRENS_HOME":
                    raise ValueError("initial benchmark seeding supports CareSignal only")
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    seed_care_ofsted_benchmark(
                        settings,
                        actor=actor,
                        benchmark_version=str(
                            payload.get("benchmark_version") or CARE_BENCHMARK_VERSION
                        ),
                        limit=min(max(int(payload.get("limit", 30)), 1), 30),
                    ),
                )
            if action == "backtesting-research-import" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    import_bundled_historical_corpus(settings, actor=actor),
                )
            if action == "backtesting-run" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                vertical = validate_vertical_filter(payload.get("vertical") or "CHILDRENS_HOME")
                if vertical == ALL_VERTICALS:
                    raise ValueError("backtest runs require one enabled vertical")
                bounds = BacktestBounds.from_values(
                    as_of=payload.get("as_of") or datetime.now(UTC).isoformat(),
                    lookback_days=payload.get("lookback_days", 365),
                    max_cases=payload.get("max_cases", 30),
                    max_signals=payload.get("max_signals", 500),
                )
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                result = execute_backtest(
                    settings,
                    actor=actor,
                    benchmark_version=str(
                        payload.get("benchmark_version") or CARE_BENCHMARK_VERSION
                    ),
                    vertical=vertical,
                    bounds=bounds,
                )
                metrics = result.get("metrics") or {}
                logger.info(
                    "backtest_run_summary run_id=%s engine=%s lookback_days=%s "
                    "usable=%s detected=%s recall=%s median_lead_days=%s idempotent=%s",
                    result.get("id"),
                    result.get("engine_version"),
                    bounds.lookback_days,
                    metrics.get("cases_usable"),
                    metrics.get("detected_cases"),
                    metrics.get("recall"),
                    (metrics.get("lead_time_days") or {}).get("median"),
                    result.get("idempotent", False),
                )
                return _response(200, result)
            if action == "backtesting-sensitivity" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                vertical = validate_vertical_filter(payload.get("vertical") or "CHILDRENS_HOME")
                if vertical != "CHILDRENS_HOME":
                    raise ValueError("initial sensitivity analysis supports CareSignal only")
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                result = execute_backtest_sensitivity(
                    settings,
                    actor=actor,
                    benchmark_version=str(
                        payload.get("benchmark_version") or CARE_BENCHMARK_VERSION
                    ),
                    vertical=vertical,
                    as_of=str(payload.get("as_of") or datetime.now(UTC).isoformat()),
                    max_cases=min(max(int(payload.get("max_cases", 30)), 1), 50),
                    max_signals=min(max(int(payload.get("max_signals", 500)), 1), 1000),
                )
                logger.info(
                    "backtest_sensitivity_summary runs=%s",
                    json.dumps(
                        [
                            {
                                "lookback_days": item["lookback_days"],
                                "usable": item["metrics"].get("cases_usable"),
                                "detected": item["metrics"].get("detected_cases"),
                                "recall": item["metrics"].get("recall"),
                                "median_lead_days": (
                                    item["metrics"].get("lead_time_days") or {}
                                ).get("median"),
                            }
                            for item in result["runs"]
                        ],
                        separators=(",", ":"),
                    ),
                )
                return _response(200, result)
            if action == "backtesting-recruitment-shadow" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                result = evaluate_current_care_recruitment(
                    settings,
                    limit=min(max(int(_query(event, "limit") or "100"), 1), 250),
                )
                logger.info(
                    "care_recruitment_shadow_summary evaluated=%s counts=%s changed=%s",
                    result["evaluated"],
                    json.dumps(result["counts"], separators=(",", ":")),
                    result["changed_count"],
                )
                return _response(200, result)
            if action == "backtesting-compare" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                left = str(UUID(str(_query(event, "left"))))
                right = str(UUID(str(_query(event, "right"))))
                return _response(200, compare_backtest_runs(settings, left, right))
            if action == "backtesting-labels" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                return _response(
                    200,
                    labelled_decisions(
                        settings,
                        vertical=validate_vertical_filter(_query(event, "vertical")),
                        limit=min(max(int(_query(event, "limit") or "100"), 1), 500),
                    ),
                )
            if action == "vertical-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                return _response(200, {"items": registry_payload()})
            if action == "organisation-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                limit = min(max(int(_query(event, "limit") or "50"), 1), 100)
                offset = max(int(_query(event, "offset") or "0"), 0)
                return _response(
                    200,
                    list_organisations(
                        settings,
                        limit=limit,
                        offset=offset,
                        vertical=validate_vertical_filter(_query(event, "vertical")),
                    ),
                )
            if action == "organisation-detail" and method == "GET" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                detail = organisation_detail(settings, signal_id)
                return _response(200, detail) if detail else _response(404, {"error": "not_found"})
            if action == "organisation-review-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                limit = min(max(int(_query(event, "limit") or "25"), 1), 100)
                return _response(
                    200, {"items": list_organisation_match_reviews(settings, limit=limit)}
                )
            if (
                action in {"organisation-review-confirm", "organisation-review-reject"}
                and method == "POST"
                and signal_id
            ):
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    resolve_organisation_match_review(
                        settings,
                        signal_id,
                        action="confirm" if action.endswith("confirm") else "reject",
                        actor=actor,
                        company_number=payload.get("company_number"),
                    ),
                )
            if action == "source-list" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                vertical = validate_vertical_filter(_query(event, "vertical"))
                sources = []
                for source_key, definition in SOURCE_DEFINITIONS.items():
                    if (
                        vertical != ALL_VERTICALS
                        and vertical not in definition["supported_verticals"]
                    ):
                        continue
                    runs = list_runs(settings, source_key, limit=10)
                    latest = runs[0] if runs else None
                    successful = next((run for run in runs if run.get("status") == "SUCCESS"), None)
                    sources.append(
                        {
                            "key": source_key,
                            "display_name": definition["display_name"],
                            "provider": definition["provider"],
                            "supported_verticals": definition["supported_verticals"],
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
            if action == "procurement-evaluation" and method == "GET":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                vertical = validate_vertical_filter(_query(event, "vertical"))
                if vertical not in {ALL_VERTICALS, "CHILDRENS_HOME"}:
                    return _response(200, {"items": [], "total": 0, "counts": {}})
                return _response(
                    200,
                    list_procurement_evaluations(
                        settings,
                        limit=min(max(int(_query(event, "limit") or "100"), 1), 200),
                        offset=max(int(_query(event, "offset") or "0"), 0),
                    ),
                )
            if action == "source-run" and method == "POST" and signal_id in SOURCE_DEFINITIONS:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                definition = SOURCE_DEFINITIONS[signal_id]
                queue_url = getattr(settings, definition["queue_setting"])
                if not queue_url:
                    return _response(503, {"error": "collector_not_configured"})
                raw_payload = _raw_body(event)
                payload_body = parse_json_payload(raw_payload) if raw_payload else {}
                vertical = validate_vertical_filter(payload_body.get("vertical"))
                if (
                    vertical != ALL_VERTICALS
                    and vertical not in definition["supported_verticals"]
                ):
                    raise ValueError("source does not support vertical")
                parameters = dict(definition["default_parameters"])
                if vertical != ALL_VERTICALS:
                    parameters["verticals"] = [vertical]
                if signal_id == "companies_house":
                    candidate_vertical = (
                        "CHILDRENS_HOME" if vertical == ALL_VERTICALS else vertical
                    )
                    parameters["organisation_candidates"] = (
                        list_organisation_enrichment_candidates(
                            settings,
                            limit=int(parameters["max_organisations"]),
                            vertical=candidate_vertical,
                        )
                    )
                run_id, started_at = start_run(
                    settings,
                    source_key=signal_id,
                    provider=definition["provider"],
                    invocation_source="manual",
                    parameters=parameters,
                )
                payload = {**parameters, "run_id": run_id, "run_started_at": started_at}
                try:
                    response = boto3.client("sqs").send_message(
                        QueueUrl=queue_url,
                        MessageBody=json.dumps(payload, separators=(",", ":")),
                    )
                    if not response.get("MessageId"):
                        raise RuntimeError("collector command was not accepted")
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
            if action == "care-backfill" and method == "POST":
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                payload = parse_json_payload(_raw_body(event))
                days = min(max(int(payload.get("days", 60)), 1), 90)
                limit = min(max(int(payload.get("limit", 25)), 1), 50)
                actor = str(claims.get("sub") or claims.get("username") or "unknown")
                return _response(
                    200,
                    backfill_care_from_stored_evidence(
                        settings, actor=actor, days=days, limit=limit
                    ),
                )
            if action == "ai-review" and method == "POST" and signal_id:
                admin_error = _require_admin(claims, settings)
                if admin_error:
                    return admin_error
                try:
                    result = reevaluate_ai_shadow(settings, signal_id)
                except UnsupportedShadowSourceError:
                    return _response(400, {"error": "ai_shadow_not_supported_for_source"})
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
                return _response(
                    200,
                    list_match_reviews(
                        settings,
                        limit=limit,
                        offset=offset,
                        vertical=validate_vertical_filter(_query(event, "vertical")),
                    ),
                )
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
                        vertical=validate_vertical_filter(_query(event, "vertical")),
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
                        settings,
                        limit=limit,
                        offset=offset,
                        search=_query(event, "q"),
                        vertical=validate_vertical_filter(_query(event, "vertical")),
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
    organisation_detail,
