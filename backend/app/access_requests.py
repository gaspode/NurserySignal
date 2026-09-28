from __future__ import annotations

import hashlib
import re
from typing import Any

from app.config import Settings
from app.db import connection

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SUPPLIER_CATEGORIES = {
    "FIT_OUT_FURNITURE",
    "RECRUITMENT",
    "TRAINING",
    "SOFTWARE",
    "TELECOMS",
    "SECURITY",
    "PROPERTY_SERVICES",
    "VEHICLES",
    "CATERING",
    "COMPLIANCE",
    "OTHER",
}


def _text(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = str(payload.get(key) or "").strip()
    if len(value) > maximum:
        raise ValueError(f"{key}_too_long")
    return value


def create_access_request(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    # A hidden browser-only field gives the public form a cheap, non-invasive bot trap.
    if _text(payload, "website", 200):
        return {"status": "accepted"}
    name = _text(payload, "name", 120)
    company = _text(payload, "company", 180)
    email = _text(payload, "email", 254).lower()
    category = _text(payload, "supplier_category", 50).upper()
    message = _text(payload, "message", 1500)
    if not name or not company or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("invalid_access_request")
    if category not in SUPPLIER_CATEGORIES:
        raise ValueError("invalid_supplier_category")
    fingerprint = hashlib.sha256(
        "\x1f".join((name.casefold(), company.casefold(), email, category, message)).encode()
    ).hexdigest()
    with connection(settings) as conn:
        row = conn.execute(
            """INSERT INTO customer_access_requests
                   (name, company, work_email, supplier_category, message,
                    request_fingerprint)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (request_fingerprint) DO UPDATE
               SET last_submitted_at = now()
               RETURNING id, created_at""",
            (name, company, email, category, message or None, fingerprint),
        ).fetchone()
        conn.commit()
    return {"status": "accepted", "request_id": str(row[0]), "received_at": row[1]}


def list_access_requests(settings: Settings, *, limit: int = 50) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, name, company, work_email, supplier_category, message,
                      status, created_at, last_submitted_at
               FROM customer_access_requests
               ORDER BY created_at DESC LIMIT %s""",
            (min(max(limit, 1), 100),),
        ).fetchall()
    return [
        {
            "id": str(row[0]),
            "name": row[1],
            "company": row[2],
            "work_email": row[3],
            "supplier_category": row[4],
            "message": row[5],
            "status": row[6],
            "created_at": row[7],
            "last_submitted_at": row[8],
        }
        for row in rows
    ]
