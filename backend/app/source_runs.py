from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3

from app.config import Settings


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_message(exc: BaseException | None) -> str | None:
    if exc is None:
        return None
    return f"{type(exc).__name__}: {str(exc)[:240]}".replace("\n", " ")


def start_run(
    settings: Settings,
    *,
    source_key: str,
    provider: str,
    invocation_source: str,
    parameters: dict[str, Any],
    run_id: str | None = None,
    started_at: str | None = None,
) -> tuple[str, str]:
    run_id = run_id or str(uuid.uuid4())
    started_at = started_at or _now()
    item = {
        "source_key": {"S": source_key},
        "run_key": {"S": f"{started_at}#{run_id}"},
        "run_id": {"S": run_id},
        "provider": {"S": provider},
        "invocation_source": {"S": invocation_source},
        "started_at": {"S": started_at},
        "status": {"S": "RUNNING"},
        "parameters": {"S": json.dumps(parameters, separators=(",", ":"), sort_keys=True)},
        "counts": {"S": json.dumps({}, separators=(",", ":"))},
    }
    try:
        if not settings.source_runs_table_name:
            return run_id, started_at
        boto3.client("dynamodb").put_item(
            TableName=settings.source_runs_table_name,
            Item=item,
        )
    except Exception:
        # Run observability must not turn a provider invocation into a retry storm.
        return run_id, started_at
    return run_id, started_at


def finish_run(
    settings: Settings,
    *,
    source_key: str,
    run_id: str,
    started_at: str,
    status: str,
    counts: dict[str, int],
    failure_category: str | None = None,
    failure_message: str | None = None,
) -> None:
    key = {"source_key": {"S": source_key}, "run_key": {"S": f"{started_at}#{run_id}"}}
    values: dict[str, Any] = {
        ":completed": {"S": _now()},
        ":status": {"S": status},
        ":counts": {"S": json.dumps(counts, separators=(",", ":"), sort_keys=True)},
    }
    assignments = ["completed_at = :completed", "#status = :status", "counts = :counts"]
    names = {"#status": "status"}
    if failure_category:
        values[":category"] = {"S": failure_category[:100]}
        assignments.append("failure_category = :category")
    if failure_message:
        values[":message"] = {"S": failure_message[:300]}
        assignments.append("failure_message = :message")
    try:
        if not settings.source_runs_table_name:
            return
        boto3.client("dynamodb").update_item(
            TableName=settings.source_runs_table_name,
            Key=key,
            UpdateExpression="SET " + ", ".join(assignments),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        return


def list_runs(settings: Settings, source_key: str, *, limit: int = 10) -> list[dict[str, Any]]:
    if not settings.source_runs_table_name:
        return []
    response = boto3.client("dynamodb").query(
        TableName=settings.source_runs_table_name,
        KeyConditionExpression="source_key = :source",
        ExpressionAttributeValues={":source": {"S": source_key}},
        ScanIndexForward=False,
        Limit=min(max(limit, 1), 25),
    )
    results = []
    for item in response.get("Items", []):

        def value(name: str, default: Any = None) -> Any:
            entry = item.get(name)
            if not entry:
                return default
            if "S" in entry:
                return entry["S"]
            return default

        try:
            counts = json.loads(value("counts", "{}"))
            parameters = json.loads(value("parameters", "{}"))
        except (TypeError, ValueError):
            counts, parameters = {}, {}
        results.append(
            {
                "id": value("run_id"),
                "source_key": source_key,
                "provider": value("provider"),
                "invocation_source": value("invocation_source"),
                "started_at": value("started_at"),
                "completed_at": value("completed_at"),
                "status": value("status"),
                "counts": counts,
                "parameters": parameters,
                "failure_category": value("failure_category"),
                "failure_message": value("failure_message"),
            }
        )
    return results


def safe_failure(exc: BaseException | None) -> tuple[str | None, str | None]:
    if exc is None:
        return None, None
    return type(exc).__name__, _safe_message(exc)
