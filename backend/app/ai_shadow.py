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
PROMPT_VERSION = "shadow-v3"
SYSTEM_PROMPT = """NurserySignal shadow review, prompt version shadow-v3.
For recruitment sources, answer the PRIMARY question only: is this a genuine,
commercially relevant recruitment signal associated with a UK nursery or
early-years setting? The primary recommendation is about relevance, not growth.
APPROVE routine and change recruitment at an identifiable nursery, preschool,
nursery school, school-based nursery or early-years setting, including Early
Years Educators, Nursery Practitioners, childcare apprentices, Nursery Managers,
Deputies, Room Leaders and relevant teaching assistants. REJECT horticultural,
plant or tree nursery roles, ordinary school roles with no nursery/EYFS relevance,
unrelated healthcare roles, or generic education roles with no meaningful
early-years setting. Use NEEDS_HUMAN only when relevance itself is genuinely
ambiguous or evidence conflicts. The absence of opening or expansion evidence
must never by itself cause REJECT for an otherwise relevant routine vacancy.
Separately classify recruitment_relevance as RELEVANT_ROUTINE, RELEVANT_CHANGE,
UNCERTAIN or IRRELEVANT, and commercial_change_evidence as NONE, WEAK or STRONG.
Routine recruitment normally means NONE; explicit new/opening/expansion wording
may mean STRONG. For non-recruitment sources, recruitment fields may be null.
Return strict JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,
"reason":"brief reason",
"recruitment_relevance":"RELEVANT_ROUTINE|RELEVANT_CHANGE|UNCERTAIN|IRRELEVANT|null",
"commercial_change_evidence":"NONE|WEAK|STRONG|null"}."""


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
    recruitment_context = metadata.get("recruitment_classification")
    if not isinstance(recruitment_context, dict):
        recruitment_context = None
    return {
        "source_type": raw.get("source_type"),
        "title": str(raw.get("title") or "")[:2000],
        "raw_text": str(raw.get("raw_text") or "")[:12000],
        "location_hint": str(raw.get("location_hint") or "")[:1000],
        "organisation_hint": str(raw.get("organisation_hint") or "")[:1000],
        "metadata": useful_metadata,
        "recruitment_context": recruitment_context,
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
        "commercial_change_evidence": None,
        "recruitment_relevance": None,
        "failure_category": category,
        "attempted_at": time.time(),
        "evaluated_at": None,
        "input_tokens": None,
        "output_tokens": None,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _parse(
    response: dict[str, Any],
    settings: Settings,
    started: float,
    source_type: str | None,
    deterministic_relevance: str | None,
) -> dict[str, Any]:
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
    change_evidence = parsed.get("commercial_change_evidence", "NONE")
    recruitment_relevance = parsed.get("recruitment_relevance")
    if recommendation not in ALLOWED_RECOMMENDATIONS or not isinstance(confidence, (int, float)):
        raise ValueError("invalid structured response")
    if not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ValueError("confidence outside range")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("missing reason")
    if change_evidence not in {"NONE", "WEAK", "STRONG"}:
        raise ValueError("invalid commercial change evidence")
    if source_type == "recruitment":
        if recruitment_relevance not in {
            "RELEVANT_ROUTINE",
            "RELEVANT_CHANGE",
            "UNCERTAIN",
            "IRRELEVANT",
        }:
            raise ValueError("invalid recruitment relevance")
        # A deterministic routine match is already known to be a relevant
        # childcare-setting signal. Shadow AI may assess its strength, but the
        # absence of growth evidence must not turn it into a rejection.
        if (
            deterministic_relevance == "RELEVANT_ROUTINE"
            and recruitment_relevance == "RELEVANT_ROUTINE"
            and recommendation != "APPROVE"
        ):
            recommendation = "APPROVE"
            reason = f"Relevant routine recruitment signal. {reason.strip()}"
    usage = response.get("usage") or {}
    return {
        "provider": "BEDROCK",
        "model_id": settings.ai_model_id,
        "prompt_version": settings.ai_prompt_version,
        "status": "SUCCEEDED",
        "recommendation": recommendation,
        "confidence": float(confidence),
        "reason": reason.strip()[:500],
        "commercial_change_evidence": change_evidence,
        "recruitment_relevance": recruitment_relevance,
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
        source_type = str(raw.get("source_type") or "").lower()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        deterministic_relevance = None
        classification = metadata.get("recruitment_classification")
        if isinstance(classification, dict):
            deterministic_relevance = classification.get("relevance")
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
        result = _parse(
            response, settings, started, source_type, deterministic_relevance
        )
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
