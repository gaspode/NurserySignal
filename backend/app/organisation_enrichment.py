from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from app.config import Settings
from app.correlation import normalize_identity
from app.db import connection
from app.storage import put_raw_evidence


def _date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _safe_candidates(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [
        {
            "company_number": item.get("company_number"),
            "company_name": item.get("company_name"),
            "company_status": item.get("company_status"),
            "registered_office_address": item.get("registered_office_address") or {},
        }
        for item in values[:10]
        if isinstance(item, dict)
    ]


def process_organisation_enrichment(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("provider") != "COMPANIES_HOUSE":
        raise ValueError("unsupported organisation enrichment provider")
    operator_id = str(payload.get("operator_id") or "")
    query_name = str(payload.get("query_name") or "").strip()
    if not operator_id or not query_name:
        raise ValueError("organisation enrichment identity is missing")
    if not settings.evidence_bucket:
        raise RuntimeError("EVIDENCE_BUCKET is not configured")
    company = payload.get("company") if isinstance(payload.get("company"), dict) else None
    candidates = _safe_candidates(payload.get("candidates"))
    status = str(payload.get("status") or "FAILED")
    outcome = str(payload.get("outcome") or "NO_MATCH")
    confidence = float(payload.get("confidence") or 0)
    reason = str(payload.get("reason") or "")[:500]
    retrieved_at = str(payload.get("retrieved_at") or "")
    evidence_payload = json.dumps(
        {
            "provider": "COMPANIES_HOUSE",
            "query_name": query_name,
            "status": status,
            "outcome": outcome,
            "confidence": confidence,
            "reason": reason,
            "company": company,
            "candidates": candidates,
            "retrieved_at": retrieved_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    content_hash = hashlib.sha256(evidence_payload).hexdigest()
    identity = (
        str(company.get("company_number"))
        if company and company.get("company_number")
        else f"operator-{operator_id}"
    )
    safe_identity = re.sub(r"[^a-zA-Z0-9-]+", "-", identity)
    key = f"organisations/companies-house/{safe_identity}/{content_hash}.json"
    put_raw_evidence(settings, settings.evidence_bucket, key, evidence_payload)
    with connection(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"operator:{operator_id}",))
        operator = conn.execute(
            "SELECT id, name, companies_house_number FROM operators WHERE id = %s FOR UPDATE",
            (operator_id,),
        ).fetchone()
        if operator is None:
            raise ValueError("organisation enrichment references an unknown operator")
        effective_status = status
        if status == "MATCHED" and company:
            company_number = str(company.get("company_number") or "").strip()
            if not company_number:
                raise ValueError("matched company profile has no company number")
            if operator[2] and str(operator[2]) != company_number:
                effective_status = "AMBIGUOUS"
                outcome = "UNCERTAIN"
                reason = "existing company number conflicts with the proposed official profile"
                candidates = [company, *candidates]
            else:
                legal_name = str(company.get("company_name") or "").strip()
                conn.execute(
                    """UPDATE operators SET legal_name = %s, companies_house_number = %s,
                       company_status = %s, incorporation_date = %s, company_type = %s,
                       registered_office = %s, sic_codes = %s, companies_house_url = %s,
                       companies_house_refreshed_at = %s::timestamptz,
                       resolution_outcome = %s, resolution_confidence = %s,
                       enrichment_provenance = %s, updated_at = now() WHERE id = %s""",
                    (
                        legal_name or operator[1],
                        company_number,
                        company.get("company_status"),
                        _date(company.get("date_of_creation")),
                        company.get("type"),
                        Jsonb(company.get("registered_office_address") or {}),
                        Jsonb(company.get("sic_codes") or []),
                        f"https://find-and-update.company-information.service.gov.uk/company/{company_number}",
                        retrieved_at,
                        outcome,
                        confidence,
                        Jsonb({"provider": "COMPANIES_HOUSE", "reason": reason}),
                        operator_id,
                    ),
                )
                for alias in {str(operator[1]), query_name, legal_name}:
                    normalized = normalize_identity(alias)
                    if normalized:
                        conn.execute(
                            """INSERT INTO organisation_aliases
                               (operator_id, alias, normalized_alias, source)
                               VALUES (%s, %s, %s, 'COMPANIES_HOUSE')
                               ON CONFLICT (operator_id, normalized_alias) DO NOTHING""",
                            (operator_id, alias, normalized),
                        )
        if effective_status == "AMBIGUOUS":
            conn.execute(
                """INSERT INTO organisation_match_reviews
                   (operator_id, provider, query_name, candidates, reason)
                   VALUES (%s, 'COMPANIES_HOUSE', %s, %s, %s)
                   ON CONFLICT (operator_id, provider) WHERE status = 'PENDING'
                   DO UPDATE SET candidates = EXCLUDED.candidates, reason = EXCLUDED.reason""",
                (operator_id, query_name, Jsonb(candidates), reason),
            )
        external_id = (
            str(company.get("company_number"))
            if company and company.get("company_number")
            else f"search:{operator_id}"
        )
        conn.execute(
            """INSERT INTO organisation_evidence
               (operator_id, provider, external_id, status, resolution_outcome,
                resolution_confidence, reason, evidence_bucket, evidence_key,
                content_sha256, retrieved_at, safe_metadata)
               VALUES (%s, 'COMPANIES_HOUSE', %s, %s, %s, %s, %s, %s, %s, %s,
                       %s::timestamptz, %s)
               ON CONFLICT (provider, external_id, content_sha256) DO NOTHING""",
            (
                operator_id,
                external_id,
                effective_status,
                outcome,
                confidence,
                reason,
                settings.evidence_bucket,
                key,
                content_hash,
                retrieved_at,
                Jsonb({"query_name": query_name, "candidate_count": len(candidates)}),
            ),
        )
        conn.commit()
    return {
        "operator_id": operator_id,
        "status": effective_status,
        "outcome": outcome,
        "company_number": company.get("company_number") if company else None,
    }
