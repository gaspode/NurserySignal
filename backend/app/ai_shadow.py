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

from app.care_planning_review import planning_withdrawal_assessment
from app.config import Settings
from app.logging import configure_logging
from app.planning_outcomes import normalize_structured_planning_value, structured_planning_values
from app.review_triage import planning_refusal_assessment

logger = configure_logging()
ALLOWED_RECOMMENDATIONS = {"APPROVE", "REJECT", "NEEDS_HUMAN"}
RECRUITMENT_PROMPT_VERSION = "recruitment-shadow-v3"
PLANNING_PROMPT_VERSION = "planning-shadow-v3"
CARE_PLANNING_PROMPT_VERSION = "care-planning-shadow-v2"
CARE_RECRUITMENT_PROMPT_VERSION = "care-recruitment-shadow-v1"
SUPPORTED_SHADOW_SOURCE_TYPES = frozenset({"planning", "recruitment"})
RECRUITMENT_SYSTEM_PROMPT = """NurserySignal recruitment shadow review,
prompt version recruitment-shadow-v3.
Answer ONLY: is this a genuine and commercially relevant recruitment signal associated
with a UK nursery or early-years setting? APPROVE routine or change recruitment at an
identifiable nursery, preschool, nursery school, school-based nursery or early-years
setting. REJECT horticultural/plant/tree nursery roles, ordinary school roles with no
nursery/EYFS relevance, unrelated healthcare roles, and generic education roles with
no meaningful early-years setting. Use NEEDS_HUMAN only when relevance itself is
genuinely ambiguous or evidence conflicts. Lack of opening/expansion evidence must
never by itself cause REJECT. Separately classify recruitment_relevance as
RELEVANT_ROUTINE, RELEVANT_CHANGE, UNCERTAIN or IRRELEVANT, and
commercial_change_evidence as NONE, WEAK or STRONG. Return strict JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,"reason":"brief reason",
"recruitment_relevance":"RELEVANT_ROUTINE|RELEVANT_CHANGE|UNCERTAIN|IRRELEVANT",
"commercial_change_evidence":"NONE|WEAK|STRONG"}."""
PLANNING_SYSTEM_PROMPT = """NurserySignal planning shadow review, prompt version planning-shadow-v3.
Answer ONLY: does this planning application provide credible evidence of new, expanded,
relocated or materially changed UK nursery/early-years provision that would be
commercially relevant to a supplier? The nursery does not need to be the primary subject
of the application. A mixed-use, residential, commercial or school development that
genuinely proposes a new nursery, day nursery, pre-school, early-years space or
purpose-built childcare facility should normally be APPROVE even when most of the
application concerns housing, infrastructure or other uses. Judge whether nursery
provision is a real proposed component, not whether it dominates the application.
APPROVE new nurseries, conversions to nursery/day-nursery use, expansions or capacity
increases, new nursery classrooms/buildings, additional provision, school-based nursery
creation/expansion, planning responding to excess nursery demand, and material childcare
alterations. Examples include "90 dwellings and a new children's nursery", "mixed-use
development including a day nursery", "new primary school and nursery provision", and
"commercial development including a purpose-built childcare facility". REJECT
horticultural/plant/tree nurseries, adult-care or children's-home uses, nursery only in an
address/property name, a nearby existing nursery used as a landmark, traffic passing an
existing nursery, former nursery premises being converted away from childcare, unrelated
education with no early-years provision, or historical/reference-only mentions with no
current nursery change. Use NEEDS_HUMAN when wording such as "nursery space" does not
establish whether actual childcare provision is proposed. Classify planning_relevance as
RELEVANT_CHANGE, RELEVANT_FOLLOWUP, RELEVANT_ROUTINE, UNCERTAIN or IRRELEVANT, and
commercial_change_evidence as NONE, WEAK or STRONG. Do not infer unsupported capacity,
operator, opening date or ownership. Do not emit recruitment_relevance. Return strict JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,"reason":"brief reason",
"planning_relevance":"RELEVANT_CHANGE|RELEVANT_FOLLOWUP|RELEVANT_ROUTINE|UNCERTAIN|IRRELEVANT",
"commercial_change_evidence":"NONE|WEAK|STRONG"}."""
CARE_PLANNING_SYSTEM_PROMPT = """CareProspect planning shadow review, prompt version
care-planning-shadow-v2. Evaluate structured council outcome/status BEFORE proposal
relevance, in this exact order:
1. If an authoritative structured decision/status is REFUSED, REFUSAL, REJECTED,
APPLICATION REFUSED, PERMISSION REFUSED, REFUSE PERMISSION, REFUSE CONSENT,
REFUSE PERMISSION/CONSENT, REFUSAL OF PERMISSION, REFUSAL OF CONSENT,
CERTIFICATE OF LAWFULNESS - REFUSED, CERTIFICATE OF LAWFULNESS REFUSED, or
PERMISSION/CONSENT REFUSED, always return recommendation REJECT and
commercial_change_evidence NONE. Strong children's-home wording never overrides a
refusal. If the subject is genuinely a children's home, use planning_relevance
RELEVANT_FOLLOWUP and explain that the subject is relevant but the council refused the
application.
2. If structured status is WITHDRAWN, APPLICATION WITHDRAWN or WITHDRAWN BY APPLICANT,
always return recommendation REJECT and commercial_change_evidence NONE. Explain that
this application is no longer active; a later resubmission must be assessed independently.
3. Otherwise decide whether this is genuinely about a UK children's residential care home
and a commercially material opening, conversion, relocation, expansion or occupancy
increase. APPROVE explicit new-home proposals and material changes.
4. Distinguish proposed lawfulness from existing-use lawfulness. Proposed lawfulness may
be APPROVE when explicit and active. Existing-use lawfulness is not a fresh opening by
default and should normally be RELEVANT_FOLLOWUP and REJECT from new-opening review.
5. Treat condition variations, condition discharges, non-material amendments and records
referencing prior permission as follow-up/supporting evidence, not a new opening, unless
the current application itself clearly creates material new provision.
REJECT adult/elderly/nursing care, generic C2 without children context, day nurseries,
hospitals, boarding schools and incidental references. Use NEEDS_HUMAN only for genuinely
ambiguous evidence. The structured outcome fields are authoritative; do not infer refusal
from free proposal text. Relevance does not equal current commercial viability. Never
include or infer resident identities or safeguarding details. Return strict JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,"reason":"brief reason",
"planning_relevance":"RELEVANT_CHANGE|RELEVANT_FOLLOWUP|RELEVANT_ROUTINE|UNCERTAIN|IRRELEVANT",
"commercial_change_evidence":"NONE|WEAK|STRONG"}."""
CARE_RECRUITMENT_SYSTEM_PROMPT = """CareSignal recruitment shadow review, prompt
version care-recruitment-shadow-v1. Decide whether this is genuine recruitment for a
UK children's residential care home. APPROVE relevant routine roles as well as opening
roles, but separately distinguish RELEVANT_ROUTINE from RELEVANT_CHANGE. REJECT adult
care, nursing homes, generic support work and education roles without children's-home
context. Lack of opening evidence must not by itself cause rejection. Never include or
infer resident identities or safeguarding details. Return strict JSON only:
{"recommendation":"APPROVE|REJECT|NEEDS_HUMAN","confidence":0.0,"reason":"brief reason",
"recruitment_relevance":"RELEVANT_ROUTINE|RELEVANT_CHANGE|UNCERTAIN|IRRELEVANT",
"commercial_change_evidence":"NONE|WEAK|STRONG"}."""


def prompt_version_for(
    source_type: str | None, settings: Settings, vertical: str | None = None
) -> str:
    """Select a source-specific version, retaining compatibility for old callers."""
    if vertical == "CHILDRENS_HOME" and source_type == "planning":
        return settings.ai_care_planning_prompt_version
    if vertical == "CHILDRENS_HOME" and source_type == "recruitment":
        return settings.ai_care_recruitment_prompt_version
    if source_type == "planning" and settings.ai_planning_prompt_version:
        return settings.ai_planning_prompt_version
    if source_type == "recruitment" and settings.ai_recruitment_prompt_version:
        return settings.ai_recruitment_prompt_version
    return settings.ai_prompt_version


def system_prompt_for(source_type: str | None, vertical: str | None = None) -> str:
    if source_type not in SUPPORTED_SHADOW_SOURCE_TYPES:
        raise ValueError("AI shadow review is not supported for this source type")
    if vertical == "CHILDRENS_HOME":
        return (
            CARE_PLANNING_SYSTEM_PROMPT
            if source_type == "planning"
            else CARE_RECRUITMENT_SYSTEM_PROMPT
        )
    return PLANNING_SYSTEM_PROMPT if source_type == "planning" else RECRUITMENT_SYSTEM_PROMPT


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
    structured_outcomes = []
    for value in structured_planning_values(metadata):
        normalized = normalize_structured_planning_value(value)
        if normalized and normalized not in structured_outcomes:
            structured_outcomes.append(normalized)
    return {
        "source_type": raw.get("source_type"),
        "vertical": raw.get("vertical"),
        "title": str(raw.get("title") or "")[:2000],
        "raw_text": str(raw.get("raw_text") or "")[:12000],
        "location_hint": str(raw.get("location_hint") or "")[:1000],
        "organisation_hint": str(raw.get("organisation_hint") or "")[:1000],
        "metadata": useful_metadata,
        "structured_planning_outcomes": structured_outcomes,
        "recruitment_context": recruitment_context,
    }


def _apply_care_planning_outcome_guard(
    raw: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    """Make exact structured refusal/withdrawal authoritative over model relevance."""
    if result.get("status") != "SUCCEEDED":
        return result
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    refusal = planning_refusal_assessment(metadata)
    withdrawal = planning_withdrawal_assessment(metadata)
    if not refusal.refused and not withdrawal.withdrawn:
        return result
    care_classification = metadata.get("care_classification")
    subject_relevant = (
        isinstance(care_classification, dict) and care_classification.get("matched") is True
    ) or str(result.get("planning_relevance") or "").startswith("RELEVANT_")
    guarded = dict(result)
    guarded["recommendation"] = "REJECT"
    guarded["commercial_change_evidence"] = "NONE"
    if subject_relevant:
        guarded["planning_relevance"] = "RELEVANT_FOLLOWUP"
    if refusal.refused:
        guarded["reason"] = (
            "Relevant children’s-home subject matter, but the authoritative structured "
            f"council outcome is {refusal.value}; the application was refused."
            if subject_relevant
            else f"The authoritative structured council outcome is {refusal.value}; "
            "the application was refused."
        )
    else:
        guarded["reason"] = (
            "Relevant children’s-home subject matter, but the authoritative structured "
            f"council outcome is {withdrawal.value}; this application is no longer active."
            if subject_relevant
            else f"The authoritative structured council outcome is {withdrawal.value}; "
            "this application is no longer active."
        )
    return guarded


def _failure(
    settings: Settings,
    category: str,
    started: float,
    source_type: str | None,
    vertical: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": "BEDROCK",
        "model_id": settings.ai_model_id,
        "prompt_version": prompt_version_for(source_type, settings, vertical),
        "status": "FAILED",
        "recommendation": None,
        "confidence": None,
        "reason": None,
        "commercial_change_evidence": None,
        "recruitment_relevance": None,
        "planning_relevance": None,
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
    vertical: str | None = None,
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
    planning_relevance = parsed.get("planning_relevance") if source_type == "planning" else None
    recruitment_relevance = (
        parsed.get("recruitment_relevance") if source_type == "recruitment" else None
    )
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
    elif source_type == "planning":
        if planning_relevance not in {
            "RELEVANT_CHANGE",
            "RELEVANT_FOLLOWUP",
            "RELEVANT_ROUTINE",
            "UNCERTAIN",
            "IRRELEVANT",
        }:
            raise ValueError("invalid planning relevance")
    usage = response.get("usage") or {}
    return {
        "provider": "BEDROCK",
        "model_id": settings.ai_model_id,
        "prompt_version": prompt_version_for(source_type, settings, vertical),
        "status": "SUCCEEDED",
        "recommendation": recommendation,
        "confidence": float(confidence),
        "reason": reason.strip()[:500],
        "commercial_change_evidence": change_evidence,
        "recruitment_relevance": recruitment_relevance,
        "planning_relevance": planning_relevance,
        "failure_category": None,
        "attempted_at": time.time(),
        "evaluated_at": time.time(),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def evaluate_shadow(raw: dict[str, Any], settings: Settings) -> dict[str, Any]:
    started = time.perf_counter()
    source_type = str(raw.get("source_type") or "").lower()
    if source_type not in SUPPORTED_SHADOW_SOURCE_TYPES:
        raise ValueError("AI shadow review is not supported for this source type")
    vertical = str(raw.get("vertical") or "NURSERY").upper()
    try:
        client = boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=3, read_timeout=20, retries={"max_attempts": 2, "mode": "standard"}
            ),
        )
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        deterministic_relevance = None
        classification = (
            metadata.get("care_recruitment_classification")
            if vertical == "CHILDRENS_HOME"
            else metadata.get("recruitment_classification")
        )
        if isinstance(classification, dict):
            deterministic_relevance = classification.get("relevance")
        response = client.converse(
            modelId=settings.ai_model_id,
            system=[{"text": system_prompt_for(source_type, vertical)}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": json.dumps(_input_for_model(raw), ensure_ascii=True)}],
                }
            ],
            inferenceConfig={"temperature": 0.0, "maxTokens": 256},
        )
        result = _parse(
            response, settings, started, source_type, deterministic_relevance, vertical
        )
        if vertical == "CHILDRENS_HOME" and source_type == "planning":
            result = _apply_care_planning_outcome_guard(raw, result)
    except (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError):
        result = _failure(settings, "TIMEOUT", started, source_type, vertical)
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
        result = _failure(settings, category, started, source_type, vertical)
    except (ValueError, json.JSONDecodeError):
        result = _failure(settings, "MALFORMED_RESPONSE", started, source_type, vertical)
    except (BotoCoreError, Exception):
        result = _failure(settings, "UNKNOWN", started, source_type, vertical)
    logger.info(
        "ai_shadow status=%s category=%s model_id=%s latency_ms=%s",
        result["status"],
        result.get("failure_category"),
        result["model_id"],
        result["latency_ms"],
    )
    return result
