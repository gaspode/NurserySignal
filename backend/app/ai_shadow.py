from __future__ import annotations

import json
import math
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.config import Settings
from app.logging import configure_logging

logger = configure_logging()
ALLOWED_RECOMMENDATIONS = {"APPROVE", "REJECT", "NEEDS_HUMAN"}
PROMPT_VERSION = "shadow-v1"
SYSTEM_PROMPT = """NurserySignal shadow review, prompt version shadow-v1.
Decide whether this UK planning/source signal is commercially useful to a supplier
of nursery and early-years equipment or services. Judge the proposed change, not
just the word nursery. APPROVE new/expanded day nurseries, pre-schools, school
nurseries, early-years accommodation or credible openings. REJECT horticultural,
plant or tree nurseries, address-only terms, nearby existing nurseries, stale
follow-ups, incidental childcare references and obvious test data. Use
NEEDS_HUMAN when evidence is ambiguous or insufficient. Return JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,"reason":"brief reason"}."""


def _input_for_model(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    useful_metadata = {
        key: metadata.get(key)
        for key in (
            "provider_application_id",
            "council",
            "postcode",
            "planning_status",
            "decision",
            "application_date",
            "provider",
        )
        if metadata.get(key) is not None
    }
    return {
        "source_type": raw.get("source_type"),
        "title": str(raw.get("title") or "")[:2000],
        "raw_text": str(raw.get("raw_text") or "")[:12000],
        "location_hint": str(raw.get("location_hint") or "")[:1000],
        "organisation_hint": str(raw.get("organisation_hint") or "")[:1000],
        "metadata": useful_metadata,
    }


def _failure(settings: Settings, category: str, started: float) -> dict[str, Any]:
    return {
        "provider": "BEDROCK",
        "model_id": settings.ai_model_id,
        "prompt_version": settings.ai_prompt_version,
        "status": "FAILED",
        "recommendation": None,
        "confidence": None,
        "reason": None,
        "failure_category": category,
        "attempted_at": time.time(),
        "evaluated_at": None,
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _parse(response: dict[str, Any], settings: Settings, started: float) -> dict[str, Any]:
    content = response.get("output", {}).get("message", {}).get("content", [])
    text = next(
        (item.get("text") for item in content if isinstance(item, dict) and item.get("text")), None
    )
    if not isinstance(text, str):
        raise ValueError("missing structured response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(cleaned)
    recommendation = parsed.get("recommendation")
    confidence = parsed.get("confidence")
    reason = parsed.get("reason")
    if recommendation not in ALLOWED_RECOMMENDATIONS or not isinstance(confidence, (int, float)):
        raise ValueError("invalid structured response")
    if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ValueError("confidence outside range")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("missing reason")
    usage = response.get("usage") or {}
    return {
        "provider": "BEDROCK",
        "model_id": settings.ai_model_id,
        "prompt_version": settings.ai_prompt_version,
        "status": "SUCCEEDED",
        "recommendation": recommendation,
        "confidence": float(confidence),
        "reason": reason.strip()[:500],
        "failure_category": None,
        "attempted_at": time.time(),
        "evaluated_at": time.time(),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def evaluate_shadow(raw: dict[str, Any], settings: Settings) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        client = boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=3, read_timeout=20, retries={"max_attempts": 2, "mode": "standard"}
            ),
        )
        response = client.converse(
            modelId=settings.ai_model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": json.dumps(_input_for_model(raw), ensure_ascii=True)}],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 256},
        )
        result = _parse(response, settings, started)
    except (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError):
        result = _failure(settings, "TIMEOUT", started)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        category = (
            "THROTTLED"
            if code
            in {"ThrottlingException", "TooManyRequestsException", "ServiceUnavailableException"}
            else "SERVICE_ERROR"
        )
        logger.warning(
            "ai_shadow bedrock_client_error code=%s http_status=%s category=%s",
            code or "UNKNOWN",
            http_status or "UNKNOWN",
            category,
        )
        result = _failure(settings, category, started)
    except (ValueError, json.JSONDecodeError):
        result = _failure(settings, "MALFORMED_RESPONSE", started)
    except (BotoCoreError, Exception):
        result = _failure(settings, "UNKNOWN", started)
    logger.info(
        "ai_shadow status=%s category=%s model_id=%s latency_ms=%s",
        result["status"],
        result.get("failure_category"),
        result["model_id"],
        result["latency_ms"],
    )
    return result
