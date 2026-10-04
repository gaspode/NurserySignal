"""Trusted backend client for the isolated official Planning-page fetch Lambda."""

from __future__ import annotations

import base64
import json
from typing import Any

import boto3

from app.config import Settings
from app.planning_party_official import OfficialPartyResult, parse_idox_party_html


def fetch_idox_party_via_fetcher(
    settings: Settings,
    source_url: str,
    *,
    lambda_client: Any | None = None,
) -> OfficialPartyResult:
    """Invoke the non-VPC fetcher; all parsing remains in the trusted backend."""
    if not settings.official_planning_fetcher_function_name:
        return OfficialPartyResult(
            "SOURCE_UNAVAILABLE", source_url, detail="fetcher_not_configured"
        )
    client = lambda_client or boto3.client("lambda")
    try:
        response = client.invoke(
            FunctionName=settings.official_planning_fetcher_function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps({"url": source_url}).encode(),
        )
        payload = json.loads(response["Payload"].read().decode("utf-8"))
    except Exception as exc:  # boto errors are mapped to a non-business source failure.
        return OfficialPartyResult("SOURCE_UNAVAILABLE", source_url, detail=type(exc).__name__)
    if payload.get("status") != "OK":
        return OfficialPartyResult(
            str(payload.get("error_category") or "SOURCE_UNAVAILABLE"),
            str(payload.get("final_url") or payload.get("requested_url") or source_url),
            detail=str(payload.get("detail") or payload.get("http_status") or "fetch_failed"),
        )
    try:
        body = base64.b64decode(str(payload["body_base64"]), validate=True).decode(
            "utf-8", errors="replace"
        )
    except (KeyError, ValueError, UnicodeError) as exc:
        return OfficialPartyResult("PARSE_FAILED", source_url, detail=type(exc).__name__)
    return parse_idox_party_html(str(payload.get("final_url") or source_url), body)
