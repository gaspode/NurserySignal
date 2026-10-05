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
from app.planning_outcomes import canonical_planning_outcome

POLICY_VERSION = "plota-contact-data-viability-v1"
NURSERY_CUSTOMER_CONTACT_POLICY_VERSION = "nursery-customer-contact-context-v1"

# These are display-governance decisions, not a claim that a value is either
# public or private.  The API terms permit customer-facing products, but make
# SignalHub independently responsible for its lawful basis, transparency and
# UK GDPR/PECR compliance for personal data.  Customer APIs must continue to
# omit every field until the policy/legal gate has been explicitly approved.
CONTACT_FIELD_DISPLAY_POLICY = {
    # These fields are already covered by the existing customer Planning
    # projection and retain the official-register source link.
    "site_address": "ALLOWED_TO_DISPLAY",
    "planning_reference": "ALLOWED_TO_DISPLAY",
    "planning_stage": "ALLOWED_TO_DISPLAY",
    "proposal_summary": "ALLOWED_TO_DISPLAY",
    "operator_status_not_confirmed": "ALLOWED_TO_DISPLAY",
    "applicant_name_person": "REQUIRES_POLICY_LEGAL_REVIEW",
    "applicant_name_company": "REQUIRES_POLICY_LEGAL_REVIEW",
    "agent_name": "REQUIRES_POLICY_LEGAL_REVIEW",
    "agent_company": "REQUIRES_POLICY_LEGAL_REVIEW",
    "applicant_email": "INTERNAL_ONLY",
    "applicant_phone": "INTERNAL_ONLY",
    "agent_email": "INTERNAL_ONLY",
    "agent_phone": "INTERNAL_ONLY",
    "case_officer_name": "INTERNAL_ONLY",
    "case_officer_email": "INTERNAL_ONLY",
    "case_officer_phone": "INTERNAL_ONLY",
}


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
    settings: Settings, *, actor: str, limit: int = 100, vertical: str = "ALL"
) -> dict[str, Any]:
    """Select approved post-2026 Planning evidence and make exact ID reads only."""
    limit = min(max(int(limit), 1), 100)
    vertical = str(vertical or "ALL").upper()
    if vertical not in {"ALL", "NURSERY", "CHILDRENS_HOME"}:
        raise ValueError("vertical must be ALL, NURSERY, or CHILDRENS_HOME")
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
               WHERE (%s = 'ALL' OR vertical = %s)
                 AND vertical_rank <= %s ORDER BY vertical, vertical_rank""",
            (vertical, vertical, limit if vertical != "ALL" else max(1, limit // 2)),
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
                "agent_company": str(contact.get("agent_company") or "").strip() or None,
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


def _contact_context(value: str | None, *, kind: str) -> str:
    """Classify commercial context without promoting a party to operator."""
    if not value:
        return "SITE_ONLY"
    if kind == "COMPANY_LIKE":
        return "ACTIONABLE_CONTACT_CONTEXT"
    return "PARTIAL_CONTACT_CONTEXT"


def _customer_planning_stage(metadata: dict[str, Any], lifecycle: Any) -> tuple[str, str]:
    """Prefer current canonical Planning outcome over legacy opportunity stage."""
    outcome = canonical_planning_outcome(metadata).outcome.value
    if outcome == "APPROVED":
        return outcome, "PLANNING_APPROVED"
    if outcome in {"PENDING", "UNKNOWN"}:
        return outcome, "PLANNING_PENDING"
    # Terminal cases are excluded by the publication predicate, but retain a
    # neutral stage if a legacy row nevertheless reaches this internal preview.
    return outcome, str(lifecycle or "UNDER_REVIEW")


def nursery_customer_contact_preview(
    settings: Settings, *, actor: str, limit: int = 50
) -> dict[str, Any]:
    """Admin-only, exact-application contact preview; never alters business state.

    The returned names are intentionally confined to an authenticated admin
    response.  This is evidence for a licensing/privacy decision, not a
    customer projection and not an operator-enrichment action.
    """
    bounded = min(max(int(limit), 1), 50)
    if not settings.planning_collector_function_name:
        raise RuntimeError("PLANNING_COLLECTOR_FUNCTION_NAME is not configured")
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT DISTINCT ON (o.id)
                         o.id, COALESCE(o.customer_title, o.name), o.customer_summary,
                         o.address, o.postcode, o.town, o.change_type, o.lifecycle_stage,
                         rs.id, rs.metadata->>'provider_application_id', rs.metadata,
                         rs.title
                  FROM opportunities o
                  JOIN opportunity_signals os ON os.opportunity_id = o.id
                    AND os.status = 'ACTIVE'
                  JOIN raw_signals rs ON rs.id = os.raw_signal_id
                  JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                  WHERE o.vertical = 'NURSERY'
                    AND o.publication_status = 'PUBLISHED'
                    AND o.change_type IN ('OPENING', 'EXPANSION')
                    AND rs.source_type = 'planning'
                    AND se.review_status = 'APPROVED'
                    AND COALESCE((rs.metadata->>'application_date')::date,
                                 rs.discovered_at::date) >= DATE '2026-01-01'
                    AND NULLIF(rs.metadata->>'provider_application_id', '') IS NOT NULL
                    AND NULLIF(COALESCE(o.address, rs.location_hint), '') IS NOT NULL
                    AND NULLIF(COALESCE(o.postcode, rs.metadata->>'postcode'), '') IS NOT NULL
                  ORDER BY o.id, rs.discovered_at DESC
                  LIMIT %s""",
            (bounded,),
        ).fetchall()
    payload = {
        "operation": "planning_contact_data_preview",
        "applications": [
            {"signal_id": str(row[8]), "provider_application_id": str(row[9])} for row in rows
        ],
    }
    response = boto3.client("lambda").invoke(
        FunctionName=settings.planning_collector_function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read() or "{}")
    if response.get("FunctionError"):
        raise RuntimeError("nursery customer contact preview fetch failed")
    by_signal = {item.get("signal_id"): item for item in body.get("results", [])}
    counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for row in rows:
        (
            opportunity_id,
            title,
            summary,
            address,
            postcode,
            town,
            change_type,
            lifecycle,
            signal_id,
            _provider_application_id,
            metadata,
            signal_title,
        ) = row
        contact = by_signal.get(str(signal_id), {})
        applicant = str(contact.get("applicant") or "").strip() or None
        agent = str(contact.get("agent") or "").strip() or None
        agent_company = str(contact.get("agent_company") or "").strip() or None
        applicant_type = "UNKNOWN"
        if applicant:
            applicant_type = "COMPANY_LIKE" if _company_like_applicant(applicant) else "PERSON"
        exact_site = bool(address and postcode)
        context = (
            _contact_context(applicant, kind=applicant_type)
            if exact_site
            else "UNSAFE_TO_DISPLAY"
        )
        if exact_site and not applicant and agent:
            context = "PARTIAL_CONTACT_CONTEXT"
        counts["applicant_present" if applicant else "applicant_absent"] += 1
        counts[f"applicant_{applicant_type.lower()}"] += 1
        counts["agent_present" if agent else "agent_absent"] += 1
        counts["agent_company_present" if agent_company else "agent_company_absent"] += 1
        counts["exact_site_address" if exact_site else "missing_exact_site_address"] += 1
        counts[context] += 1
        metadata = metadata or {}
        planning_outcome, planning_stage = _customer_planning_stage(metadata, lifecycle)
        counts[f"planning_outcome_{planning_outcome.lower()}"] += 1
        samples.append(
            {
                "opportunity_id": str(opportunity_id),
                "contact_context": context,
                "site_project": {
                    "title": str(title),
                    "site_address": str(address) if address else None,
                    "postcode": str(postcode) if postcode else None,
                    "town": str(town) if town else None,
                    "planning_authority": metadata.get("council") or metadata.get("authority"),
                    "planning_reference": metadata.get("planning_reference"),
                    "planning_outcome": planning_outcome,
                    "planning_stage": planning_stage,
                    "proposal_change_type": str(change_type or "OTHER_CHANGE"),
                    "customer_safe_summary": str(summary or signal_title or "")[:500],
                },
                "contact_provenance": {
                    "applicant_name": applicant,
                    "applicant_role": "APPLICANT" if applicant else None,
                    "applicant_type": applicant_type,
                    "agent_name": agent,
                    "agent_company": agent_company,
                    "agent_role": "AGENT" if (agent or agent_company) else None,
                    "operator_status": "NOT_CONFIRMED",
                    "source": "PLOTA_CONTACT_DATA",
                    "source_paths": {
                        "applicant_name": "data.applicant" if applicant else None,
                        "agent_name": "data.agent" if agent else None,
                        "agent_company": "data.agent_company" if agent_company else None,
                    },
                },
                # Internal mock-up only.  It is deliberately labelled blocked
                # until the separate legal/policy gate is approved.
                "customer_safe_preview": {
                    "title": str(title),
                    "site_address": str(address) if address else None,
                    "postcode": str(postcode) if postcode else None,
                    "planning_reference": metadata.get("planning_reference"),
                    "planning_stage": planning_stage,
                    "proposal_summary": str(summary or signal_title or "")[:500],
                    "applicant": applicant,
                    "planning_agent": agent_company or agent,
                    "operator": "Not confirmed",
                    "display_gate": "BLOCKED_PENDING_POLICY_LEGAL_REVIEW",
                },
                "contact_error": contact.get("error_category"),
            }
        )
    return {
        "schema_version": "signalhub-nursery-customer-contact-preview-v1",
        "policy_version": NURSERY_CUSTOMER_CONTACT_POLICY_VERSION,
        "preview_only": True,
        "customer_exposure": "DISABLED_PENDING_POLICY_LEGAL_REVIEW",
        "actor": actor,
        "examined": len(samples),
        "provider_requests": body.get("provider_requests", 0),
        "field_display_policy": CONTACT_FIELD_DISPLAY_POLICY,
        "counts": dict(counts),
        "samples": samples,
    }
