from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError

from app.config import Settings
from app.customer import PLAN_ENTITLEMENTS, digest_preview
from app.db import connection

logger = logging.getLogger("nurserysignal")


def _safe_provider_error(exc: Exception) -> tuple[str, str]:
    if not isinstance(exc, ClientError):
        return type(exc).__name__, ""
    error = exc.response.get("Error", {})
    code = str(error.get("Code") or "ClientError")[:80]
    message = str(error.get("Message") or "")[:240]
    message = re.sub(r"[^\s@]+@[^\s,;]+", "[redacted-email]", message)
    return code, message


def _period(now: datetime) -> tuple[datetime, datetime]:
    end = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=7), end


def queue_weekly_digests(
    settings: Settings, *, now: datetime | None = None, limit: int = 250
) -> dict[str, int]:
    if not settings.caresignal_email_from or not settings.customer_digest_queue_url:
        return {"eligible": 0, "queued": 0, "duplicates": 0, "errors": 0}
    start, end = _period(now or datetime.now(UTC))
    with connection(settings) as conn:
        users = conn.execute(
            """SELECT u.id, u.account_id, u.email, u.display_name, u.role, u.status,
                      a.name, a.status, a.plan, a.allowed_regions,
                      a.allowed_local_authorities
               FROM customer_users u
               JOIN customer_accounts a ON a.id = u.account_id
               JOIN customer_alert_preferences p ON p.customer_user_id = u.id
               WHERE u.status = 'ACTIVE' AND a.status <> 'SUSPENDED'
                 AND p.frequency = 'WEEKLY'
               ORDER BY u.id LIMIT %s""",
            (min(max(limit, 1), 250),),
        ).fetchall()
    counts = {"eligible": len(users), "queued": 0, "duplicates": 0, "errors": 0}
    sqs = boto3.client("sqs")
    for row in users:
        context = {
            "user_id": str(row[0]),
            "account_id": str(row[1]),
            "email": row[2],
            "display_name": row[3],
            "role": row[4],
            "user_status": row[5],
            "account_name": row[6],
            "account_status": row[7],
            "plan": row[8],
            "allowed_regions": list(row[9] or []),
            "allowed_local_authorities": list(row[10] or []),
            "entitlements": PLAN_ENTITLEMENTS[str(row[8])],
        }
        try:
            preview = digest_preview(settings, context, period_start=start)
            with connection(settings) as conn:
                run = conn.execute(
                    """INSERT INTO customer_digest_runs
                       (account_id, customer_user_id, frequency, period_start,
                        period_end, status, opportunity_count)
                       VALUES (%s, %s, 'WEEKLY', %s, %s, 'PREVIEWED', %s)
                       ON CONFLICT (customer_user_id, frequency, period_start, period_end)
                       DO NOTHING RETURNING id""",
                    (
                        context["account_id"],
                        context["user_id"],
                        start,
                        end,
                        len(preview["opportunities"]),
                    ),
                ).fetchone()
                conn.commit()
            if not run:
                counts["duplicates"] += 1
                continue
            message = {
                "run_id": str(run[0]),
                "to": context["email"],
                "subject": preview["subject"],
                "html": preview["html"],
            }
            sqs.send_message(
                QueueUrl=settings.customer_digest_queue_url,
                MessageBody=json.dumps(message, separators=(",", ":")),
            )
            update_digest_delivery(settings, str(run[0]), status="QUEUED")
            counts["queued"] += 1
        except Exception as exc:
            counts["errors"] += 1
            logger.error(
                "customer_digest_queue_failed user_id=%s error_type=%s",
                context["user_id"],
                type(exc).__name__,
            )
    logger.info(
        "customer_weekly_digest_summary eligible=%s queued=%s duplicates=%s errors=%s",
        counts["eligible"],
        counts["queued"],
        counts["duplicates"],
        counts["errors"],
    )
    return counts


def update_digest_delivery(
    settings: Settings, run_id: str, *, status: str, safe_failure: str | None = None
) -> None:
    if status not in {"QUEUED", "SENT", "FAILED"}:
        raise ValueError("invalid digest status")
    with connection(settings) as conn:
        conn.execute(
            """UPDATE customer_digest_runs SET status = %s, safe_failure = %s,
                 sent_at = CASE WHEN %s = 'SENT' THEN now() ELSE sent_at END
               WHERE id = %s""",
            (status, safe_failure[:240] if safe_failure else None, status, run_id),
        )
        conn.commit()


def sender_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    sender = os.environ.get("CARESIGNAL_EMAIL_FROM", "").strip()
    sender_name = os.environ.get("CARESIGNAL_EMAIL_FROM_NAME", "CareProspect").strip()
    backend_function = os.environ.get("BACKEND_FUNCTION_NAME", "").strip()
    ses = boto3.client("sesv2")
    lambda_client = boto3.client("lambda")
    failures = []
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            payload = json.loads(record["body"])
            if not sender or not payload.get("to") or not payload.get("run_id"):
                raise ValueError("digest sender is not configured")
            ses.send_email(
                FromEmailAddress=f"{sender_name} <{sender}>" if sender_name else sender,
                Destination={"ToAddresses": [payload["to"]]},
                Content={
                    "Simple": {
                        "Subject": {"Data": payload["subject"]},
                        "Body": {"Html": {"Data": payload["html"]}},
                    }
                },
            )
            _notify_delivery(lambda_client, backend_function, payload["run_id"], "SENT")
        except Exception as exc:
            error_code, safe_message = _safe_provider_error(exc)
            logger.error(
                "customer_digest_send_failed error_code=%s provider_message=%s",
                error_code,
                safe_message,
            )
            try:
                body = json.loads(record.get("body") or "{}")
                if body.get("run_id"):
                    _notify_delivery(
                        lambda_client,
                        backend_function,
                        body["run_id"],
                        "FAILED",
                        type(exc).__name__,
                    )
            finally:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def _notify_delivery(
    client: Any,
    function_name: str,
    run_id: str,
    status: str,
    safe_failure: str | None = None,
) -> None:
    client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "operation": "customer_digest_delivery",
                "run_id": run_id,
                "status": status,
                "safe_failure": safe_failure,
            }
        ).encode(),
    )
