"""Bounded, read-only commercial usefulness preview for Plota Contact Data."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

import boto3

from app.config import Settings
from app.correlation import normalize_identity
from app.customer import _company_like_applicant
from app.db import connection

POLICY_VERSION = "plota-contact-data-viability-v1"


def _identity_index(conn: Any) -> dict[str, list[dict[str, str]]]:
    rows = conn.execute(
        """SELECT o.id, o.name, a.alias FROM operators o
           LEFT JOIN organisation_aliases a ON a.operator_id = o.id"""
    ).fetchall()
    index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for operator_id, name, alias in rows:
        for value in (name, alias):
            key = normalize_identity(value)
            if key:
                index[key].append({"id": str(operator_id), "name": str(name)})
    return index


def planning_contact_data_preview(
    settings: Settings, *, actor: str, limit: int = 100
) -> dict[str, Any]:
    """Select approved post-2026 Planning evidence and make exact ID reads only."""
    limit = min(max(int(limit), 1), 100)
    if not settings.planning_collector_function_name:
        raise RuntimeError("PLANNING_COLLECTOR_FUNCTION_NAME is not configured")
    with connection(settings) as conn:
        rows = conn.execute(
            """WITH candidates AS (
                 SELECT rs.id, rs.vertical, rs.metadata->>'provider_application_id',
                        rs.title, rs.raw_text, rs.metadata, se.extracted_facts,
                        row_number() OVER (PARTITION BY rs.vertical ORDER BY
                          CASE WHEN se.review_status = 'APPROVED' THEN 0 ELSE 1 END,
                          rs.discovered_at DESC) AS vertical_rank
                 FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id=rs.id
                 WHERE rs.source_type='planning' AND se.review_status='APPROVED'
                   AND COALESCE(
                         (rs.metadata->>'application_date')::date,
                         rs.discovered_at::date
                       ) >= DATE '2026-01-01'
                   AND NULLIF(rs.metadata->>'provider_application_id','') IS NOT NULL
               ) SELECT * FROM candidates
               WHERE vertical_rank <= %s ORDER BY vertical, vertical_rank""",
            (max(1, limit // 2),),
        ).fetchall()
        index = _identity_index(conn)
    selected = rows[:limit]
    payload = {
        "operation": "planning_contact_data_preview",
        "applications": [
            {"signal_id": str(row[0]), "provider_application_id": str(row[2])} for row in selected
        ],
    }
    response = boto3.client("lambda").invoke(
        FunctionName=settings.planning_collector_function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read() or "{}")
    if response.get("FunctionError"):
        raise RuntimeError("planning contact preview fetch failed")
    by_signal = {item.get("signal_id"): item for item in body.get("results", [])}
    counts: Counter[str] = Counter()
    identity_counts: Counter[str] = Counter()
    items = []
    for row in selected:
        signal_id, vertical, _provider_id, title, raw_text, metadata, facts, _rank = row
        contact = by_signal.get(str(signal_id), {})
        applicant = str(contact.get("applicant") or "").strip() or None
        agent = str(contact.get("agent") or "").strip() or None
        applicant_kind = (
            "ABSENT"
            if not applicant
            else "COMPANY_LIKE"
            if _company_like_applicant(applicant)
            else "PERSON"
        )
        match = [] if not applicant else index.get(normalize_identity(applicant), [])
        match = {item["id"]: item for item in match}
        match_outcome = (
            "EXACT_EXISTING_ORGANISATION"
            if len(match) == 1 and applicant_kind == "COMPANY_LIKE"
            else "AMBIGUOUS_MATCH"
            if len(match) > 1
            else "NO_LOCAL_MATCH"
        )
        role = (
            "APPLICANT_PERSON"
            if applicant_kind == "PERSON"
            else "LIKELY_OPERATOR"
            if match_outcome == "EXACT_EXISTING_ORGANISATION"
            else "POSSIBLE_OPERATOR"
            if applicant_kind == "COMPANY_LIKE"
            else "UNKNOWN_ROLE"
        )
        counts["applicant_present" if applicant else "no_contact"] += 1
        counts[f"applicant_{applicant_kind.lower()}"] += 1
        counts["agent_present" if agent else "agent_absent"] += 1
        identity_counts[match_outcome] += 1
        items.append(
            {
                "signal_id": str(signal_id),
                "vertical": vertical,
                "title": title,
                "planning_reference": (metadata or {}).get("planning_reference"),
                "applicant": applicant,
                "applicant_normalized": normalize_identity(applicant),
                "applicant_classification": applicant_kind,
                "agent": agent,
                "agent_normalized": normalize_identity(agent),
                "organisation_match": match_outcome,
                "organisation_candidates": list(match.values()),
                "likely_role": role,
                "contact_error": contact.get("error_category"),
            }
        )
    repeated = {
        key: value
        for key, value in Counter(
            item["applicant_normalized"] for item in items if item["applicant_normalized"]
        ).items()
        if value > 1
    }
    return {
        "schema_version": "signalhub-plota-contact-data-viability-v1",
        "policy_version": POLICY_VERSION,
        "preview_only": True,
        "actor": actor,
        "examined": len(items),
        "provider_requests": body.get("provider_requests", 0),
        "counts": dict(counts),
        "organisation_match_counts": dict(identity_counts),
        "repeated_applicants": repeated,
        "recruitment_correlation": {
            "status": "NOT_EVALUATED",
            "reason": (
                "applicant is not operator evidence; exact organisation resolution "
                "is required before strict correlation"
            ),
        },
        "items": items,
    }
