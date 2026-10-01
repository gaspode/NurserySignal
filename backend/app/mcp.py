from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
import jwt
from jwt import PyJWK, PyJWKClient, PyJWTError
from psycopg.types.json import Jsonb

from app.config import Settings
from app.db import connection
from app.operations import operations_summary
from app.repository import (
    care_opportunity_hygiene_audit,
    care_policy_readonly_items,
    list_match_reviews,
    list_opportunities,
    list_signals,
    opportunity_detail,
    signal_detail,
)
from app.source_runs import list_runs

logger = logging.getLogger(__name__)

SERVER_VERSION = "signalhub-mcp-v1"
SCHEMA_VERSION = "signalhub-mcp-tools-v1"
READ_SCOPE = "signalhub-mcp/read"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
CHATGPT_CIMD_URL = "https://chatgpt.com/oauth/client.json"
CHATGPT_REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_CALLBACK_CIMD_PATH = re.compile(r"^/oauth/([A-Za-z0-9_-]{1,200})/client\.json$")
CHATGPT_CIMD = {
    "client_id": CHATGPT_CIMD_URL,
    "client_name": "ChatGPT",
    "redirect_uris": [CHATGPT_REDIRECT_URI],
    "grant_types": ["authorization_code"],
    "response_types": ["code"],
    "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
    "token_endpoint_auth_method": "private_key_jwt",
}
_jwk_clients: dict[str, PyJWKClient] = {}
SOURCE_SCHEDULES = {
    "planning": {"enabled": True, "schedule": "rate(1 day)", "provider": "Plota"},
    "recruitment": {
        "enabled": True,
        "schedule": "rate(1 day)",
        "provider": "GOV.UK Apprenticeships",
    },
    "ofsted": {"enabled": False, "schedule": "manual", "provider": "Ofsted"},
    "companies_house": {
        "enabled": False,
        "schedule": "manual",
        "provider": "Companies House",
    },
    "procurement": {
        "enabled": False,
        "schedule": "manual shadow",
        "provider": "Find a Tender + Contracts Finder",
    },
}


class MCPError(Exception):
    def __init__(self, code: str, message: str, *, rpc_code: int = -32602) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rpc_code = rpc_code


def _schema(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


PAGING = {
    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
    "cursor": {"type": "string", "description": "Opaque continuation cursor."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_operations_summary",
        "description": "Current bounded SignalHub operational health summary.",
        "inputSchema": _schema(),
    },
    {
        "name": "search_signals",
        "description": "Search bounded signal evidence and review state.",
        "inputSchema": _schema(
            {
                **PAGING,
                "vertical": {"type": "string", "enum": ["ALL", "NURSERY", "CHILDRENS_HOME"]},
                "review_status": {
                    "type": "string",
                    "enum": ["PENDING", "APPROVED", "REJECTED", "REVIEWED"],
                },
                "source_type": {"type": "string"},
                "q": {"type": "string", "maxLength": 200},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "matched": {"type": "boolean"},
            }
        ),
    },
    {
        "name": "get_signal",
        "description": "Inspect one signal using a redacted admin evidence projection.",
        "inputSchema": _schema({"signal_id": {"type": "string", "format": "uuid"}}, ["signal_id"]),
    },
    {
        "name": "search_opportunities",
        "description": (
            "Search opportunities by lifecycle, publication, policy, attention, or text."
        ),
        "inputSchema": _schema(
            {
                **PAGING,
                "vertical": {"type": "string", "enum": ["ALL", "NURSERY", "CHILDRENS_HOME"]},
                "lifecycle": {"type": "string"},
                "publication_state": {"type": "string"},
                "publication_policy_outcome": {"type": "string"},
                "withdrawal_policy_outcome": {"type": "string"},
                "needs_attention_category": {"type": "string"},
                "q": {"type": "string", "maxLength": 200},
            }
        ),
    },
    {
        "name": "get_opportunity",
        "description": (
            "Explain one opportunity's lifecycle, evidence, publication, withdrawal, "
            "watcher and history state."
        ),
        "inputSchema": _schema(
            {"opportunity_id": {"type": "string", "format": "uuid"}}, ["opportunity_id"]
        ),
    },
    {
        "name": "get_needs_attention",
        "description": "Return the existing authoritative Needs Attention cohort.",
        "inputSchema": _schema(
            {**PAGING, "category": {"type": "string"}, "q": {"type": "string", "maxLength": 200}}
        ),
    },
    {
        "name": "get_review_backlog",
        "description": "Summarise or list review, QA, matching and unmatched queues.",
        "inputSchema": _schema(
            {
                **PAGING,
                "queue": {
                    "type": "string",
                    "enum": [
                        "summary",
                        "pending_signals",
                        "publication_manual",
                        "publication_qa",
                        "match_review",
                        "unmatched_strong",
                        "lifecycle_needs_review",
                    ],
                },
                "vertical": {"type": "string", "enum": ["ALL", "NURSERY", "CHILDRENS_HOME"]},
            }
        ),
    },
    {
        "name": "get_source_status",
        "description": "Read collector/source run health without triggering providers.",
        "inputSchema": _schema(
            {
                "source": {
                    "type": "string",
                    "enum": [
                        "all",
                        "planning",
                        "recruitment",
                        "ofsted",
                        "companies_house",
                        "procurement",
                    ],
                }
            }
        ),
    },
    {
        "name": "get_automation_status",
        "description": "Read watcher, publication and withdrawal runtime status and quotas.",
        "inputSchema": _schema(),
    },
    {
        "name": "get_recent_changes",
        "description": "Bounded operational timeline from persisted history only.",
        "inputSchema": _schema(
            {
                **PAGING,
                "last_hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 24},
                "since": {"type": "string"},
                "until": {"type": "string"},
            }
        ),
    },
]

for _tool in TOOLS:
    _tool["outputSchema"] = {"type": "object", "additionalProperties": True}
    _tool["annotations"] = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    _tool["securitySchemes"] = [{"type": "oauth2", "scopes": [READ_SCOPE]}]
    _tool["_meta"] = {"securitySchemes": [{"type": "oauth2", "scopes": [READ_SCOPE]}]}


def _json_default(value: Any) -> str | float:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def _response(status: int, body: Any, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    values = {"content-type": "application/json", "cache-control": "no-store"}
    values.update(headers or {})
    return {
        "statusCode": status,
        "headers": values,
        "body": json.dumps(body, default=_json_default, separators=(",", ":")),
    }


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, error: MCPError) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": error.rpc_code, "message": error.message, "data": {"error": error.code}},
    }


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "===").decode())
        offset = int(value["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise MCPError("invalid_argument", "Invalid cursor") from exc


def _cursor(offset: int, limit: int, total: int) -> str | None:
    if offset + limit >= total:
        return None
    return (
        base64.urlsafe_b64encode(
            json.dumps({"offset": offset + limit}, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )


def _paging(args: dict[str, Any]) -> tuple[int, int]:
    limit = int(args.get("limit", DEFAULT_LIMIT))
    if not 1 <= limit <= MAX_LIMIT:
        raise MCPError("invalid_argument", f"limit must be between 1 and {MAX_LIMIT}")
    return limit, _offset(args.get("cursor"))


def _uuid(value: Any, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError) as exc:
        raise MCPError("invalid_argument", f"{name} must be a UUID") from exc


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith("https://") and len(text) <= 2000 else None


def _postcode_district(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text.split()[0] if text else None


def _safe_signal(item: dict[str, Any]) -> dict[str, Any]:
    facts = item.get("extracted_facts") or {}
    return {
        "signal_id": str(item.get("id")),
        "vertical": item.get("vertical"),
        "source_type": item.get("source_type"),
        "source_url": _safe_url(item.get("source_url")),
        "external_id": item.get("external_id"),
        "title": str(item.get("title") or "")[:500],
        "discovered_at": item.get("discovered_at"),
        "review_status": item.get("review_status"),
        "classification": {
            "event_type": item.get("event_type"),
            "lifecycle_stage": item.get("lifecycle_stage"),
            "confidence": item.get("confidence"),
            "planning_subtype": facts.get("planning_subtype"),
            "opportunity_creation_decision": facts.get("opportunity_creation_decision"),
            "planning_outcome": facts.get("planning_outcome"),
        },
        "ai": {
            "status": item.get("ai_status"),
            "recommendation": item.get("ai_recommendation"),
            "confidence": item.get("ai_confidence"),
            "prompt_version": item.get("ai_prompt_version"),
        },
    }


def _tool_operations(settings: Settings, _args: dict[str, Any]) -> dict[str, Any]:
    return operations_summary(settings)


def _tool_search_signals(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    limit, offset = _paging(args)
    matched = args.get("matched")
    result = list_signals(
        settings,
        limit=limit,
        offset=offset,
        review_status=args.get("review_status"),
        source_type=args.get("source_type"),
        discovered_from=args.get("since"),
        discovered_to=args.get("until"),
        search=args.get("q"),
        unmatched_only=matched is False,
        include_excluded=False,
        vertical=args.get("vertical") or "ALL",
    )
    items = [_safe_signal(item) for item in result["items"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "items": items,
        "count": len(items),
        "total": result["total"],
        "next_cursor": _cursor(offset, limit, result["total"]),
    }


def _tool_get_signal(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    signal_id = _uuid(args.get("signal_id"), "signal_id")
    item = signal_detail(settings, signal_id)
    if item is None:
        raise MCPError("not_found", "Signal not found", rpc_code=-32004)
    base = _safe_signal(
        {
            **item,
            **(item.get("enrichment") or {}),
            "id": item.get("id"),
            "source_type": item.get("source_type"),
            "source_url": item.get("source_url"),
            "external_id": item.get("external_id"),
            "title": item.get("title"),
            "vertical": item.get("vertical"),
        }
    )
    with connection(settings) as conn:
        related = conn.execute(
            """SELECT o.id, o.name, os.status
               FROM opportunity_signals os
               JOIN opportunities o ON o.id = os.opportunity_id
               WHERE os.raw_signal_id = %s
               ORDER BY os.created_at DESC LIMIT 10""",
            (signal_id,),
        ).fetchall()
    enrichment = item.get("enrichment") or {}
    base.update(
        {
            "evidence": {
                "document_count": len(item.get("documents") or []),
                "revision_count": len(item.get("planning_revisions") or []),
                "summary": [str(v)[:500] for v in (enrichment.get("evidence") or [])[:10]]
                if isinstance(enrichment.get("evidence"), list)
                else [],
            },
            "review": {
                "status": enrichment.get("review_status"),
                "reviewed_at": enrichment.get("reviewed_at"),
            },
            "ai_assessments": [
                {
                    k: row.get(k)
                    for k in (
                        "status",
                        "recommendation",
                        "confidence",
                        "reason",
                        "prompt_version",
                        "evaluated_at",
                    )
                }
                for row in (item.get("ai_reviews") or [])[:5]
            ],
            "related_opportunities": [
                {"opportunity_id": str(row[0]), "name": row[1], "relationship_status": row[2]}
                for row in related
            ],
        }
    )
    return {"schema_version": SCHEMA_VERSION, "signal": base}


def _policy_map(settings: Settings) -> dict[str, dict[str, Any]]:
    return {item["opportunity_id"]: item for item in care_policy_readonly_items(settings)}


def _tool_search_opportunities(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    limit, offset = _paging(args)
    policy = (
        _policy_map(settings)
        if any(
            args.get(key)
            for key in (
                "publication_policy_outcome",
                "withdrawal_policy_outcome",
                "needs_attention_category",
            )
        )
        else {}
    )
    raw = list_opportunities(
        settings,
        limit=5000 if policy or args.get("lifecycle") or args.get("publication_state") else limit,
        offset=0 if policy or args.get("lifecycle") or args.get("publication_state") else offset,
        search=args.get("q"),
        vertical=args.get("vertical") or "ALL",
    )
    items = raw["items"]
    lifecycle = args.get("lifecycle")
    publication = args.get("publication_state")
    if lifecycle:
        items = [
            item
            for item in items
            if item.get("customer_lifecycle_stage", item.get("lifecycle_stage")) == lifecycle
        ]
    if publication:
        items = [item for item in items if item.get("publication_status") == publication]
    for item in items:
        item.update(policy.get(str(item["id"]), {}))
    if args.get("publication_policy_outcome"):
        items = [
            item
            for item in items
            if item.get("publication_outcome") == args["publication_policy_outcome"]
        ]
    if args.get("withdrawal_policy_outcome"):
        items = [
            item
            for item in items
            if item.get("withdrawal_outcome") == args["withdrawal_policy_outcome"]
        ]
    if args.get("needs_attention_category"):
        items = [
            item
            for item in items
            if item.get("hygiene_category") == args["needs_attention_category"]
        ]
    total = len(items) if (policy or lifecycle or publication) else raw["total"]
    if policy or lifecycle or publication:
        items = items[offset : offset + limit]
    safe = [
        {
            "opportunity_id": str(item["id"]),
            "name": item.get("name"),
            "vertical": item.get("vertical"),
            "change_type": item.get("change_type"),
            "lifecycle": item.get("customer_lifecycle_stage", item.get("lifecycle_stage")),
            "publication_state": item.get("publication_status"),
            "town": item.get("town"),
            "postcode_district": _postcode_district(item.get("postcode")),
            "signal_count": item.get("signal_count"),
            "publication_policy": {
                "outcome": item.get("publication_outcome"),
                "reason": item.get("publication_reason"),
            },
            "withdrawal_policy": {
                "outcome": item.get("withdrawal_outcome"),
                "reason": item.get("withdrawal_reason"),
            },
            "needs_attention": {
                "category": item.get("hygiene_category"),
                "reason": item.get("hygiene_reason"),
            },
        }
        for item in items
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "items": safe,
        "count": len(safe),
        "total": total,
        "next_cursor": _cursor(offset, limit, total),
    }


def _tool_get_opportunity(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    opportunity_id = _uuid(args.get("opportunity_id"), "opportunity_id")
    item = opportunity_detail(settings, opportunity_id)
    if item is None:
        raise MCPError("not_found", "Opportunity not found", rpc_code=-32004)
    policy = (
        _policy_map(settings).get(opportunity_id, {})
        if item.get("vertical") == "CHILDRENS_HOME"
        else {}
    )
    signals = [
        {
            "signal_id": str(signal.get("id")),
            "source_type": signal.get("source_type"),
            "title": str(signal.get("title") or "")[:500],
            "review_status": signal.get("review_status"),
            "relationship_status": signal.get("relationship_status"),
            "source_url": _safe_url(signal.get("source_url")),
            "planning_outcome": signal.get("planning_outcome"),
            "planning_subtype": signal.get("planning_subtype"),
            "opportunity_action": signal.get("opportunity_creation_decision"),
            "evidence_role": signal.get("evidence_support"),
        }
        for signal in (item.get("signals") or [])[:100]
    ]
    result = {
        "opportunity_id": opportunity_id,
        "name": item.get("name"),
        "vertical": item.get("vertical"),
        "change_type": item.get("change_type"),
        "location": {
            "town": item.get("town"),
            "postcode_district": _postcode_district(item.get("postcode")),
        },
        "organisation": {
            "operator_id": str(item.get("operator_id")) if item.get("operator_id") else None,
            "operator_name": item.get("operator_name"),
        },
        "lifecycle": {
            "current": item.get("customer_lifecycle_stage"),
            "reason": item.get("customer_lifecycle_reason"),
            "policy_version": item.get("customer_lifecycle_policy_version"),
            "history": item.get("lifecycle_history", [])[:25],
        },
        "evidence_support": item.get("evidence_support"),
        "linked_signals": signals,
        "publication": {
            "state": item.get("publication_status"),
            "published_at": item.get("customer_published_at"),
            "automatic_provenance": item.get("publication_automation_provenance") or {},
            "automation_blocked": item.get("publication_automation_blocked"),
            "automation_block_reason": item.get("publication_automation_reason"),
            "policy_outcome": policy.get("publication_outcome"),
            "policy_reason": policy.get("publication_reason"),
            "exclusion_codes": policy.get("publication_exclusions", []),
        },
        "withdrawal": {
            "policy_outcome": policy.get("withdrawal_outcome"),
            "policy_reason": policy.get("withdrawal_reason"),
        },
        "needs_attention": {
            "category": policy.get("hygiene_category"),
            "reason": policy.get("hygiene_reason"),
        },
        "planning_watches": item.get("planning_lifecycle_watches", [])[:20],
        "explanation": {
            "why_not_auto_published": policy.get("publication_reason")
            if policy.get("publication_outcome") != "AUTO_PUBLISH_ELIGIBLE"
            else None
        },
    }
    return {"schema_version": SCHEMA_VERSION, "opportunity": result}


def _tool_needs_attention(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    limit, offset = _paging(args)
    result = care_opportunity_hygiene_audit(
        settings,
        limit=limit,
        offset=offset,
        category=args.get("category"),
        q=args.get("q"),
        view="needs_attention",
    )
    items = [
        {
            "opportunity_id": str(item.get("opportunity_id")),
            "name": item.get("name"),
            "category": item.get("category"),
            "reason": item.get("reason"),
            "root_cause": item.get("root_cause"),
            "publication_status": item.get("publication_status"),
            "change_type": item.get("change_type"),
            "signal_count": item.get("signal_count"),
            "foundational_signal_count": item.get("foundational_signal_count"),
        }
        for item in result["items"]
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "category_counts": result.get("category_counts"),
        "items": items,
        "count": len(items),
        "total": result["filtered_total"],
        "next_cursor": _cursor(offset, limit, result["filtered_total"]),
    }


def _tool_review_backlog(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    queue = args.get("queue", "summary")
    limit, offset = _paging(args)
    vertical = args.get("vertical") or "ALL"
    summary = operations_summary(settings)
    if queue == "summary":
        return {
            "schema_version": SCHEMA_VERSION,
            "review": summary.get("signals"),
            "matching": summary.get("opportunities"),
            "publication": summary.get("publication"),
            "data_quality": summary.get("data_quality"),
        }
    if queue == "pending_signals":
        result = list_signals(
            settings, limit=limit, offset=offset, review_status="PENDING", vertical=vertical
        )
        items = [_safe_signal(item) for item in result["items"]]
        total = result["total"]
    elif queue == "match_review":
        result = list_match_reviews(settings, limit=limit, offset=offset, vertical=vertical)
        items, total = result["items"], result["total"]
    elif queue == "unmatched_strong":
        result = list_signals(
            settings,
            limit=limit,
            offset=offset,
            review_status="APPROVED",
            unmatched_only=True,
            opportunity_decision="CREATE_OPPORTUNITY",
            vertical=vertical,
        )
        items, total = [_safe_signal(item) for item in result["items"]], result["total"]
    else:
        policies = care_policy_readonly_items(settings)
        wanted = {"publication_manual": "MANUAL_REVIEW", "publication_qa": "QA_HOLDOUT"}.get(queue)
        if queue == "lifecycle_needs_review":
            result = list_opportunities(settings, limit=5000, offset=0, vertical="CHILDRENS_HOME")
            rows = [
                item
                for item in result["items"]
                if item.get("customer_lifecycle_stage") == "NEEDS_REVIEW"
            ]
            items = [
                {
                    "opportunity_id": str(item["id"]),
                    "name": item.get("name"),
                    "lifecycle": "NEEDS_REVIEW",
                }
                for item in rows[offset : offset + limit]
            ]
            total = len(rows)
        else:
            rows = [item for item in policies if item["publication_outcome"] == wanted]
            items, total = rows[offset : offset + limit], len(rows)
    return {
        "schema_version": SCHEMA_VERSION,
        "queue": queue,
        "items": items,
        "count": len(items),
        "total": total,
        "next_cursor": _cursor(offset, limit, total),
    }


def _tool_source_status(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    requested = args.get("source", "all")
    keys = (
        [requested]
        if requested != "all"
        else ["planning", "recruitment", "ofsted", "companies_house", "procurement"]
    )
    items = []
    for key in keys:
        runs = list_runs(settings, key, limit=10)
        latest = runs[0] if runs else None
        success = next((run for run in runs if run.get("status") == "SUCCESS"), None)
        items.append(
            {
                "source": key,
                **SOURCE_SCHEDULES[key],
                "last_attempt_at": latest.get("started_at") if latest else None,
                "last_success_at": success.get("completed_at") if success else None,
                "last_status": latest.get("status") if latest else None,
                "recent_counts": latest.get("counts", {}) if latest else {},
                "recent_error": {
                    "category": latest.get("failure_category"),
                    "message": str(latest.get("failure_message") or "")[:240],
                }
                if latest and latest.get("failure_category")
                else None,
            }
        )
    return {"schema_version": SCHEMA_VERSION, "items": items}


def _tool_automation(settings: Settings, _args: dict[str, Any]) -> dict[str, Any]:
    summary = operations_summary(settings)
    return {
        "schema_version": SCHEMA_VERSION,
        "watcher": summary.get("planning_watcher"),
        "publication": summary.get("publication"),
        "withdrawal": summary.get("withdrawal"),
    }


def _tool_recent(settings: Settings, args: dict[str, Any]) -> dict[str, Any]:
    limit, offset = _paging(args)
    hours = int(args.get("last_hours", 24))
    if not 1 <= hours <= 720:
        raise MCPError("invalid_argument", "last_hours must be between 1 and 720")
    since = args.get("since") or (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    until = args.get("until") or datetime.now(UTC).isoformat()
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT change_type, entity_id, happened_at, summary FROM (
              SELECT 'LIFECYCLE_TRANSITION' change_type, opportunity_id entity_id,
                created_at happened_at, jsonb_build_object(
                  'from', old_lifecycle, 'to', new_lifecycle, 'reason', reason,
                  'policy_version', policy_version) summary
              FROM opportunity_lifecycle_history
              UNION ALL SELECT 'OPPORTUNITY_CREATED', id, created_at,
                jsonb_build_object('name', name, 'vertical', vertical,
                                   'change_type', change_type)
              FROM opportunities
              UNION ALL SELECT 'SIGNAL_REVIEWED', raw_signal_id, reviewed_at,
                jsonb_build_object('status', review_status, 'reviewed_by', reviewed_by)
              FROM signal_enrichments WHERE reviewed_at IS NOT NULL
              UNION ALL SELECT 'AUTOMATIC_PUBLICATION', i.opportunity_id, i.created_at,
                jsonb_build_object('status', i.status, 'policy_version', r.policy_version)
              FROM care_publication_run_items i
              JOIN care_publication_runs r ON r.id = i.run_id
              UNION ALL SELECT 'AUTOMATIC_WITHDRAWAL', i.opportunity_id, i.created_at,
                jsonb_build_object('status', i.status, 'policy_version', r.policy_version,
                                   'reason', i.reason)
              FROM care_withdrawal_run_items i
              JOIN care_withdrawal_runs r ON r.id = i.run_id
              UNION ALL SELECT 'ADMIN_AUDIT', id, created_at,
                jsonb_build_object('action', action, 'target_type', target_type)
              FROM admin_audit_events
              WHERE action IN ('opportunities_merged', 'opportunity_superseded')
            ) changes
            WHERE happened_at >= %s::timestamptz AND happened_at <= %s::timestamptz
            ORDER BY happened_at DESC LIMIT %s OFFSET %s""",
            (since, until, limit + 1, offset),
        ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "schema_version": SCHEMA_VERSION,
        "since": since,
        "until": until,
        "items": [
            {
                "change_type": row[0],
                "entity_id": str(row[1]),
                "timestamp": row[2],
                "summary": row[3],
            }
            for row in rows
        ],
        "count": len(rows),
        "next_cursor": _cursor(offset, limit, offset + limit + (1 if has_more else 0))
        if has_more
        else None,
    }


HANDLERS: dict[str, Callable[[Settings, dict[str, Any]], dict[str, Any]]] = {
    "get_operations_summary": _tool_operations,
    "search_signals": _tool_search_signals,
    "get_signal": _tool_get_signal,
    "search_opportunities": _tool_search_opportunities,
    "get_opportunity": _tool_get_opportunity,
    "get_needs_attention": _tool_needs_attention,
    "get_review_backlog": _tool_review_backlog,
    "get_source_status": _tool_source_status,
    "get_automation_status": _tool_automation,
    "get_recent_changes": _tool_recent,
}


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def _decode_access_token(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.mcp_token_issuer:
        raise MCPError("unavailable", "MCP token issuer is not configured", rpc_code=-32003)
    try:
        unverified = jwt.decode(
            token,
            options={"verify_signature": False, "verify_aud": False},
            algorithms=["RS256"],
        )
        client_id = str(unverified.get("client_id") or "")
        audience = unverified.get("aud")
        is_service = bool(
            settings.mcp_service_client_id and client_id == settings.mcp_service_client_id
        )
        if not is_service and audience != settings.mcp_resource_url:
            raise MCPError("unauthorized", "MCP token audience is invalid", rpc_code=-32001)
        issuer = settings.mcp_token_issuer.rstrip("/")
        if settings.mcp_token_jwks:
            try:
                jwks_document = json.loads(settings.mcp_token_jwks)
                key_id = jwt.get_unverified_header(token).get("kid")
                jwk = next(item for item in jwks_document["keys"] if item.get("kid") == key_id)
                key = PyJWK.from_dict(jwk).key
            except (json.JSONDecodeError, KeyError, StopIteration, TypeError, ValueError) as exc:
                raise MCPError(
                    "unauthorized", "OAuth access token signing key is invalid", rpc_code=-32001
                ) from exc
        else:
            jwks = _jwk_clients.setdefault(
                issuer, PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)
            )
            key = jwks.get_signing_key_from_jwt(token).key
        options = {"require": ["exp", "iat", "iss"], "verify_aud": not is_service}
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=settings.mcp_resource_url if not is_service else None,
            options=options,
        )
    except MCPError:
        raise
    except PyJWTError as exc:
        raise MCPError("unauthorized", "OAuth access token is invalid", rpc_code=-32001) from exc


def _authenticate(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    authorization = _headers(event).get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise MCPError("unauthorized", "OAuth bearer token required", rpc_code=-32001)
    claims = _decode_access_token(token.strip(), settings)
    if claims.get("token_use") != "access":
        raise MCPError("unauthorized", "OAuth access token required", rpc_code=-32001)
    scopes = set(str(claims.get("scope") or "").split())
    if READ_SCOPE not in scopes:
        raise MCPError("unauthorized", "MCP read scope required", rpc_code=-32001)
    client_id = str(claims.get("client_id") or claims.get("aud") or "")
    groups = _claim_groups(claims)
    is_service = bool(
        settings.mcp_service_client_id and client_id == settings.mcp_service_client_id
    )
    is_admin_user = bool(
        settings.mcp_user_client_id
        and client_id == settings.mcp_user_client_id
        and settings.admin_group in groups
    )
    if not (is_service or is_admin_user):
        raise MCPError("unauthorized", "MCP admin/service authorization required", rpc_code=-32001)
    return {
        "client_id": str(claims.get("sub") or client_id)[:200],
        "scopes": scopes,
        "auth_kind": "service" if is_service else "admin_user",
    }


def _claim_groups(claims: dict[str, Any]) -> set[str]:
    values = claims.get("cognito:groups") or ""
    if isinstance(values, str):
        return {value.strip() for value in values.strip("[]").split(",") if value.strip()}
    return {str(value) for value in values}


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    if not settings.mcp_resource_url or not settings.mcp_oauth_issuer:
        raise MCPError("unavailable", "MCP OAuth metadata is not configured", rpc_code=-32003)
    return {
        "resource": settings.mcp_resource_url,
        "authorization_servers": [settings.mcp_oauth_issuer],
        "scopes_supported": [READ_SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{settings.mcp_resource_url}/capabilities",
    }


def authorization_server_metadata(settings: Settings) -> dict[str, Any]:
    if not settings.mcp_oauth_issuer or not settings.mcp_oauth_authorization_server:
        raise MCPError("unavailable", "MCP OAuth metadata is not configured", rpc_code=-32003)
    issuer = settings.mcp_oauth_issuer.rstrip("/")
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [READ_SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def _auth_challenge(settings: Settings) -> str:
    resource = str(settings.mcp_resource_url or "").rstrip("/")
    origin = resource.removesuffix("/mcp")
    metadata_url = f"{origin}/.well-known/oauth-protected-resource"
    return (
        f'Bearer resource_metadata="{metadata_url}", scope="{READ_SCOPE}", '
        'error="invalid_token", error_description="OAuth authorization is required"'
    )


def _start_audit(
    settings: Settings, client: dict[str, Any], tool: str, args: dict[str, Any]
) -> str:
    metadata = {
        "keys": sorted(args),
        "limit": args.get("limit"),
        "has_query": bool(args.get("q")),
        "entity_id": args.get("signal_id") or args.get("opportunity_id"),
    }
    with connection(settings) as conn:
        recent = conn.execute(
            """SELECT count(*) FROM mcp_request_audit
               WHERE client_id = %s AND created_at >= now() - interval '1 minute'""",
            (client["client_id"],),
        ).fetchone()[0]
        if int(recent) >= settings.mcp_rate_limit_per_minute:
            raise MCPError("unavailable", "MCP rate limit exceeded", rpc_code=-32003)
        row = conn.execute(
            """INSERT INTO mcp_request_audit
                 (client_id, scope, tool_name, argument_metadata)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (client["client_id"], READ_SCOPE, tool, Jsonb(metadata)),
        ).fetchone()
        conn.commit()
    return str(row[0])


def _finish_audit(
    settings: Settings, audit_id: str, *, success: bool, error_code: str | None, duration_ms: int
) -> None:
    try:
        with connection(settings) as conn:
            conn.execute(
                """UPDATE mcp_request_audit
                   SET success = %s, error_code = %s, duration_ms = %s,
                       completed_at = now()
                   WHERE id = %s""",
                (success, error_code, duration_ms, audit_id),
            )
            conn.commit()
    except Exception:
        logger.exception("mcp_audit_completion_failed")


def capabilities(settings: Settings) -> dict[str, Any]:
    return {
        "server_version": SERVER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "environment": settings.environment,
        "read_only": True,
        "required_scope": READ_SCOPE,
        "authentication": "OAuth 2.1 CIMD authorization code + PKCE or scoped service token",
        "authorization_server": settings.mcp_oauth_issuer,
        "client_identification": "CIMD",
        "chatgpt_client_id": CHATGPT_CIMD_URL,
        "redirect_uri": CHATGPT_REDIRECT_URI,
        "tools": [tool["name"] for tool in TOOLS],
    }


def _query(event: dict[str, Any]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in (event.get("queryStringParameters") or {}).items()
        if value is not None
    }


def _redirect(location: str) -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {"location": location, "cache-control": "no-store"},
        "body": "",
    }


def _fetch_chatgpt_cimd(client_id: str = CHATGPT_CIMD_URL) -> dict[str, Any]:
    """Resolve one of ChatGPT's documented CIMD client identifiers.

    The stable client is pinned so the private MCP data plane never needs
    Internet egress. Callback-specific clients are resolved only by the
    Internet-facing OAuth facade, with a strict chatgpt.com URL allowlist to
    prevent this metadata lookup becoming an SSRF primitive.
    """
    parsed = urllib.parse.urlparse(client_id)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "chatgpt.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise MCPError("invalid_argument", "Unsupported OAuth client")
    if client_id == CHATGPT_CIMD_URL:
        return dict(CHATGPT_CIMD)
    match = CHATGPT_CALLBACK_CIMD_PATH.fullmatch(parsed.path)
    if not match:
        raise MCPError("invalid_argument", "Unsupported OAuth client")

    request = urllib.request.Request(
        client_id,
        headers={"Accept": "application/json", "User-Agent": "SignalHub-MCP-OAuth/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            content_type = response.headers.get_content_type()
            payload = response.read(65_537)
    except (OSError, urllib.error.URLError) as exc:
        raise MCPError("unavailable", "OAuth client metadata unavailable", rpc_code=-32003) from exc
    if content_type != "application/json" or len(payload) > 65_536:
        raise MCPError("invalid_argument", "Invalid OAuth client metadata")
    try:
        metadata = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPError("invalid_argument", "Invalid OAuth client metadata") from exc
    # The URL is the CIMD client identifier. Authorization-request semantics
    # are validated below; at registration time the security-critical client
    # binding is the exact redirect allowlist published by ChatGPT.
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("redirect_uris"), list)
        or not metadata["redirect_uris"]
    ):
        logger.warning(
            "mcp_oauth_cimd_rejected client_id=%s fields=%s redirect_uris_type=%s",
            client_id,
            sorted(str(key) for key in metadata) if isinstance(metadata, dict) else [],
            type(metadata.get("redirect_uris")).__name__
            if isinstance(metadata, dict)
            else type(metadata).__name__,
        )
        raise MCPError("invalid_argument", "OAuth client metadata rejected")
    return metadata


def _validate_chatgpt_client(
    client_id: str, redirect_uri: str | None = None
) -> dict[str, Any]:
    metadata = _fetch_chatgpt_cimd(client_id)
    if redirect_uri is not None:
        parsed = urllib.parse.urlparse(redirect_uri)
        callback_path = re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]{1,200}", parsed.path)
        safe_redirect = redirect_uri == CHATGPT_REDIRECT_URI or (
            parsed.scheme == "https"
            and parsed.hostname == "chatgpt.com"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and callback_path is not None
        )
        if not safe_redirect or redirect_uri not in metadata.get("redirect_uris", []):
            logger.warning(
                "mcp_oauth_redirect_rejected client_id=%s redirect_uri=%s safe=%s advertised=%s",
                client_id,
                redirect_uri,
                safe_redirect,
                len(metadata.get("redirect_uris", [])),
            )
            raise MCPError("invalid_argument", "Redirect URI rejected")
    return metadata


def _store_oauth_transaction(
    settings: Settings,
    *,
    state: str,
    original_state: str,
    redirect_uri: str,
    client_id: str,
    resource: str,
) -> None:
    if settings.mcp_oauth_transactions_table_name:
        boto3.client("dynamodb").put_item(
            TableName=settings.mcp_oauth_transactions_table_name,
            Item={
                "state": {"S": state},
                "original_state": {"S": original_state},
                "redirect_uri": {"S": redirect_uri},
                "client_id": {"S": client_id},
                "resource": {"S": resource},
                "expires_at": {"N": str(int(time.time()) + 600)},
            },
            ConditionExpression="attribute_not_exists(#state)",
            ExpressionAttributeNames={"#state": "state"},
        )
        return
    with connection(settings) as conn:
        conn.execute("DELETE FROM mcp_oauth_transactions WHERE expires_at < now()")
        conn.execute(
            """INSERT INTO mcp_oauth_transactions
                 (state, original_state, redirect_uri, client_id, resource, expires_at)
               VALUES (%s, %s, %s, %s, %s, now() + interval '10 minutes')""",
            (state, original_state, redirect_uri, client_id, resource),
        )
        conn.commit()


def _consume_oauth_transaction(settings: Settings, state: str) -> dict[str, str] | None:
    if settings.mcp_oauth_transactions_table_name:
        response = boto3.client("dynamodb").delete_item(
            TableName=settings.mcp_oauth_transactions_table_name,
            Key={"state": {"S": state}},
            ReturnValues="ALL_OLD",
        )
        item = response.get("Attributes") or {}
        if not item or int(item.get("expires_at", {}).get("N", "0")) < int(time.time()):
            return None
        return {
            "original_state": item["original_state"]["S"],
            "redirect_uri": item["redirect_uri"]["S"],
            "client_id": item["client_id"]["S"],
            "resource": item["resource"]["S"],
        }
    with connection(settings) as conn:
        row = conn.execute(
            """DELETE FROM mcp_oauth_transactions
               WHERE state = %s AND expires_at >= now()
               RETURNING original_state, redirect_uri, client_id, resource""",
            (state,),
        ).fetchone()
        conn.commit()
    if not row:
        return None
    return {
        "original_state": str(row[0]),
        "redirect_uri": str(row[1]),
        "client_id": str(row[2]),
        "resource": str(row[3]),
    }


def _oauth_error_redirect(
    settings: Settings, redirect_uri: str, state: str | None, error: str, description: str
) -> dict[str, Any]:
    values = {
        "error": error,
        "error_description": description,
        "iss": str(settings.mcp_oauth_issuer).rstrip("/"),
    }
    if state:
        values["state"] = state
    return _redirect(f"{redirect_uri}?{urllib.parse.urlencode(values)}")


def _oauth_authorize(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    args = _query(event)
    required = {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "resource",
    }
    if required - args.keys():
        return _response(400, {"error": "invalid_request", "message": "Missing OAuth parameter"})
    try:
        _validate_chatgpt_client(args["client_id"], args["redirect_uri"])
    except MCPError as exc:
        status = 503 if exc.code == "unavailable" else 400
        error = "temporarily_unavailable" if status == 503 else "invalid_client"
        return _response(status, {"error": error, "message": exc.message})
    if args["response_type"] != "code" or args["code_challenge_method"] != "S256":
        return _oauth_error_redirect(
            settings,
            args["redirect_uri"],
            args.get("state"),
            "invalid_request",
            "Code with PKCE S256 required",
        )
    scopes = set(args["scope"].split())
    if READ_SCOPE not in scopes or scopes - {READ_SCOPE}:
        return _oauth_error_redirect(
            settings,
            args["redirect_uri"],
            args.get("state"),
            "invalid_scope",
            "Unsupported OAuth scope",
        )
    if args["resource"] != settings.mcp_resource_url:
        return _oauth_error_redirect(
            settings,
            args["redirect_uri"],
            args.get("state"),
            "invalid_target",
            "MCP resource mismatch",
        )
    if not settings.mcp_user_client_id or not settings.mcp_oauth_callback_url:
        raise MCPError("unavailable", "OAuth facade is not configured", rpc_code=-32003)
    transaction_state = secrets.token_urlsafe(32)
    _store_oauth_transaction(
        settings,
        state=transaction_state,
        original_state=args["state"],
        redirect_uri=args["redirect_uri"],
        client_id=args["client_id"],
        resource=args["resource"],
    )
    provider_args = {
        "response_type": "code",
        "client_id": settings.mcp_user_client_id,
        "redirect_uri": settings.mcp_oauth_callback_url,
        "scope": READ_SCOPE,
        "state": transaction_state,
        "code_challenge": args["code_challenge"],
        "code_challenge_method": "S256",
        "resource": settings.mcp_resource_url,
    }
    provider = settings.mcp_oauth_authorization_server.rstrip("/")
    return _redirect(f"{provider}/oauth2/authorize?{urllib.parse.urlencode(provider_args)}")


def _oauth_callback(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    args = _query(event)
    state = args.get("state")
    if not state:
        return _response(400, {"error": "invalid_request", "message": "OAuth state missing"})
    item = _consume_oauth_transaction(settings, state)
    if not item:
        return _response(400, {"error": "invalid_request", "message": "OAuth state expired"})
    redirect_uri = str(item["redirect_uri"])
    original_state = str(item["original_state"])
    issuer = str(settings.mcp_oauth_issuer).rstrip("/")
    response: dict[str, str] = {"state": original_state, "iss": issuer}
    if args.get("code"):
        response["code"] = args["code"]
    else:
        response["error"] = args.get("error", "server_error")
        response["error_description"] = args.get(
            "error_description", "Authorization did not complete"
        )
    return _redirect(f"{redirect_uri}?{urllib.parse.urlencode(response)}")


def _request_body(event: dict[str, Any]) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode()
    return str(body)


def _oauth_token(event: dict[str, Any], settings: Settings) -> dict[str, Any]:
    parsed = urllib.parse.parse_qs(_request_body(event), keep_blank_values=True)
    args = {key: values[-1] for key, values in parsed.items() if values}
    grant_type = args.get("grant_type")
    if grant_type not in {"authorization_code", "refresh_token"}:
        return _response(400, {"error": "unsupported_grant_type"})
    try:
        redirect_uri = (
            str(args.get("redirect_uri") or "")
            if grant_type == "authorization_code"
            else None
        )
        _validate_chatgpt_client(str(args.get("client_id") or ""), redirect_uri)
    except MCPError as exc:
        status = 503 if exc.code == "unavailable" else 401
        error = "temporarily_unavailable" if status == 503 else "invalid_client"
        return _response(status, {"error": error})
    if args.get("resource") != settings.mcp_resource_url:
        return _response(400, {"error": "invalid_target"})
    if grant_type == "authorization_code":
        if not args.get("code") or not args.get("code_verifier"):
            return _response(400, {"error": "invalid_request"})
    if not settings.mcp_user_client_id or not settings.mcp_oauth_callback_url:
        raise MCPError("unavailable", "OAuth facade is not configured", rpc_code=-32003)
    provider_args = {
        "grant_type": grant_type,
        "client_id": settings.mcp_user_client_id,
        "resource": settings.mcp_resource_url,
    }
    if grant_type == "authorization_code":
        provider_args.update(
            {
                "code": args["code"],
                "code_verifier": args["code_verifier"],
                "redirect_uri": settings.mcp_oauth_callback_url,
            }
        )
    else:
        if not args.get("refresh_token"):
            return _response(400, {"error": "invalid_request"})
        provider_args["refresh_token"] = args["refresh_token"]
    token_url = f"{settings.mcp_oauth_authorization_server.rstrip('/')}/oauth2/token"
    request = urllib.request.Request(
        token_url,
        data=urllib.parse.urlencode(provider_args).encode(),
        headers={
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "user-agent": "SignalHub-MCP/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(65_536))
    except urllib.error.HTTPError as exc:
        try:
            provider_error = json.loads(exc.read(16_384))
        except json.JSONDecodeError:
            provider_error = {"error": "temporarily_unavailable"}
        return _response(exc.code if 400 <= exc.code < 500 else 503, provider_error)
    except (OSError, json.JSONDecodeError) as exc:
        raise MCPError("unavailable", "OAuth token service unavailable", rpc_code=-32003) from exc
    access_token = payload.get("access_token")
    if not isinstance(access_token, str):
        raise MCPError("unavailable", "OAuth token response invalid", rpc_code=-32003)
    claims = _decode_access_token(access_token, settings)
    if claims.get("client_id") != settings.mcp_user_client_id:
        raise MCPError("unauthorized", "OAuth token client is invalid", rpc_code=-32001)
    if settings.admin_group not in _claim_groups(claims):
        return _response(403, {"error": "access_denied", "message": "Admin access required"})
    return _response(200, payload)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    request_id: Any = None
    method = str(event.get("requestContext", {}).get("http", {}).get("method") or "POST").upper()
    path = str(event.get("rawPath") or "/mcp")
    if method == "GET" and path in {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/mcp/.well-known/oauth-protected-resource",
    }:
        try:
            return _response(200, protected_resource_metadata(settings))
        except MCPError as exc:
            return _response(503, {"error": exc.code, "message": exc.message})
    if method == "GET" and path == "/oauth/authorize":
        try:
            return _oauth_authorize(event, settings)
        except MCPError as exc:
            return _response(503, {"error": exc.code, "message": exc.message})
    if method == "GET" and path == "/oauth/callback":
        try:
            return _oauth_callback(event, settings)
        except MCPError as exc:
            return _response(503, {"error": exc.code, "message": exc.message})
    if method == "POST" and path == "/oauth/token":
        try:
            return _oauth_token(event, settings)
        except MCPError as exc:
            status = 401 if exc.code == "unauthorized" else 503
            return _response(status, {"error": exc.code, "message": exc.message})
    if method == "GET" and path == "/.well-known/oauth-authorization-server":
        try:
            return _response(200, authorization_server_metadata(settings))
        except MCPError as exc:
            return _response(503, {"error": exc.code, "message": exc.message})
    if method == "GET" and path == "/.well-known/openid-configuration":
        return _response(
            404,
            {
                "error": "not_supported",
                "message": "Use OAuth authorization-server metadata for this OAuth-only facade",
            },
        )
    try:
        client = _authenticate(event, settings)
    except MCPError as exc:
        return _response(
            401 if exc.code == "unauthorized" else 503,
            {"error": exc.code, "message": exc.message},
            headers={"www-authenticate": _auth_challenge(settings)},
        )
    if method == "GET" and path == "/mcp/capabilities":
        return _response(200, capabilities(settings))
    if method != "POST" or path != "/mcp":
        return _response(
            405, {"error": "invalid_argument", "message": "Use POST /mcp or GET /mcp/capabilities"}
        )
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        request = json.loads(body)
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            raise MCPError("invalid_argument", "jsonrpc must be 2.0", rpc_code=-32600)
        rpc_method = request.get("method")
        if rpc_method == "initialize":
            return _response(
                200,
                _rpc_result(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "SignalHub MCP", "version": SERVER_VERSION},
                        "instructions": (
                            "Read-only SignalHub administration. No tool can mutate "
                            "business state or call providers."
                        ),
                    },
                ),
            )
        if rpc_method == "notifications/initialized":
            return {"statusCode": 202, "headers": {"cache-control": "no-store"}, "body": ""}
        if rpc_method == "tools/list":
            return _response(200, _rpc_result(request_id, {"tools": TOOLS}))
        if rpc_method != "tools/call":
            raise MCPError("invalid_argument", "Method not found", rpc_code=-32601)
        params = request.get("params") or {}
        tool = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if tool not in HANDLERS:
            raise MCPError("invalid_argument", "Unknown tool", rpc_code=-32601)
        if not isinstance(args, dict):
            raise MCPError("invalid_argument", "Tool arguments must be an object")
        started = time.monotonic()
        audit_id = _start_audit(settings, client, tool, args)
        try:
            value = HANDLERS[tool](settings, args)
            _finish_audit(
                settings,
                audit_id,
                success=True,
                error_code=None,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return _response(
                200,
                _rpc_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    value, default=_json_default, separators=(",", ":")
                                ),
                            }
                        ],
                        "structuredContent": value,
                        "isError": False,
                    },
                ),
            )
        except MCPError as exc:
            _finish_audit(
                settings,
                audit_id,
                success=False,
                error_code=exc.code,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return _response(
                200,
                _rpc_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"error": exc.code, "message": exc.message}),
                            }
                        ],
                        "isError": True,
                    },
                ),
            )
        except (ValueError, TypeError) as exc:
            _finish_audit(
                settings,
                audit_id,
                success=False,
                error_code="invalid_argument",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return _response(
                200,
                _rpc_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": "invalid_argument", "message": str(exc)[:240]}
                                ),
                            }
                        ],
                        "isError": True,
                    },
                ),
            )
        except Exception:
            logger.exception("mcp_tool_failed tool=%s client=%s", tool, client["client_id"])
            _finish_audit(
                settings,
                audit_id,
                success=False,
                error_code="internal_error",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return _response(
                200,
                _rpc_result(
                    request_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": "internal_error", "message": "Tool execution failed"}
                                ),
                            }
                        ],
                        "isError": True,
                    },
                ),
            )
    except (json.JSONDecodeError, MCPError) as exc:
        error = (
            exc
            if isinstance(exc, MCPError)
            else MCPError("invalid_argument", "Invalid JSON", rpc_code=-32700)
        )
        return _response(400, _rpc_error(request_id, error))
