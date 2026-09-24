from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.db import check_connection
from app.logging import configure_logging

logger = configure_logging()


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = Settings.from_env()
    path = event.get("rawPath") or event.get("path") or "/"
    method = (event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    logger.info("request path=%s method=%s environment=%s", path, method, settings.environment)

    if path == "/health" and method == "GET":
        db_configured = settings.database_url is not None or settings.db_secret_arn is not None
        db_ok = check_connection(settings) if db_configured else False
        status = "ok" if db_ok else "degraded"
        return _response(200 if db_ok else 503, {
            "status": status,
            "service": settings.service_name,
            "database": "connected" if db_ok else "unavailable",
        })

    if path == "/" and method == "GET":
        return _response(200, {"service": settings.service_name, "status": "ready"})

    return _response(404, {"error": "not_found"})
