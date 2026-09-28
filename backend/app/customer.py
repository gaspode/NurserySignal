from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from psycopg.types.json import Jsonb

from app.config import Settings
from app.db import connection

PLAN_ENTITLEMENTS = {
    "STARTER": {
        "nationwide": False,
        "saved_searches": False,
        "alert_frequencies": ["OFF", "WEEKLY"],
        "full_timeline": True,
        "csv_export": False,
        "api_access": False,
    },
    "PRO": {
        "nationwide": True,
        "saved_searches": True,
        "alert_frequencies": ["OFF", "WEEKLY", "DAILY", "IMMEDIATE"],
        "full_timeline": True,
        "csv_export": False,
        "api_access": False,
    },
    "BUSINESS": {
        "nationwide": True,
        "saved_searches": True,
        "alert_frequencies": ["OFF", "WEEKLY", "DAILY", "IMMEDIATE"],
        "full_timeline": True,
        "csv_export": True,
        "api_access": False,
    },
}

STAGES = {
    "DISCOVERED": "Early signal",
    "PLANNING": "Planning",
    "STAFFING": "Recruiting",
    "RECRUITING": "Recruiting",
    "REGISTRATION": "Registration",
    "APPROVED": "Planning approved",
    "FIT_OUT": "Preparing to open",
    "OPENING_SOON": "Opening soon",
    "OPEN": "Open / confirmed",
}
CHANGE_LABELS = {
    "OPENING": "New opening",
    "EXPANSION": "Expansion",
    "RELOCATION": "Relocation",
    "OTHER_CHANGE": "Material change",
}
ALLOWED_EVENT_TYPES = {
    "LOGIN",
    "OPPORTUNITY_VIEWED",
    "OPPORTUNITY_SAVED",
    "OPPORTUNITY_UNSAVED",
    "SEARCH_USED",
    "FILTER_USED",
    "SOURCE_LINK_CLICKED",
    "DIGEST_OPENED",
    "DIGEST_CLICKED",
}


def _clean_list(values: Any, *, maximum: int = 50) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError("invalid list")
    return sorted({str(value).strip()[:100] for value in values if str(value).strip()})


def customer_context(settings: Settings, claims: dict[str, Any]) -> dict[str, Any] | None:
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        return None
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT u.id, u.account_id, u.email, u.display_name, u.role, u.status,
                      a.name, a.status, a.plan, a.allowed_regions,
                      a.allowed_local_authorities
               FROM customer_users u JOIN customer_accounts a ON a.id = u.account_id
               WHERE u.cognito_sub = %s""",
            (subject,),
        ).fetchone()
    if not row:
        return None
    plan = str(row[8])
    return {
        "user_id": str(row[0]),
        "account_id": str(row[1]),
        "email": row[2],
        "display_name": row[3],
        "role": row[4],
        "user_status": row[5],
        "account_name": row[6],
        "account_status": row[7],
        "plan": plan,
        "allowed_regions": list(row[9] or []),
        "allowed_local_authorities": list(row[10] or []),
        "entitlements": PLAN_ENTITLEMENTS[plan],
    }


def _require_active(context: dict[str, Any]) -> None:
    if context["user_status"] != "ACTIVE" or context["account_status"] == "SUSPENDED":
        raise PermissionError("customer_account_suspended")


def _opportunity_geography_sql() -> str:
    return """LEFT JOIN LATERAL (
        SELECT
          (array_agg(NULLIF(COALESCE(rs.metadata->>'region',
              rs.metadata->>'provider_region'), '')) FILTER
              (WHERE COALESCE(
                  rs.metadata->>'region', rs.metadata->>'provider_region'
              ) IS NOT NULL))[1]
              AS region,
          (array_agg(NULLIF(COALESCE(rs.metadata->>'local_authority',
              rs.metadata->>'council', rs.metadata->'authority'->>'name'), '')) FILTER
              (WHERE COALESCE(rs.metadata->>'local_authority', rs.metadata->>'council',
                  rs.metadata->'authority'->>'name') IS NOT NULL))[1] AS local_authority,
          array_agg(DISTINCT rs.source_type) FILTER
              (WHERE rs.source_type <> 'procurement') AS source_types
        FROM opportunity_signals os
        JOIN raw_signals rs ON rs.id = os.raw_signal_id
        WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
    ) geo ON TRUE"""


def _eligibility_sql() -> str:
    return """o.vertical = 'CHILDRENS_HOME'
        AND o.publication_status = 'PUBLISHED'
        AND o.review_status NOT IN ('REJECTED', 'MERGED')
        AND o.merged_into_opportunity_id IS NULL
        AND EXISTS (
          SELECT 1 FROM opportunity_signals eligible_os
          JOIN raw_signals eligible_rs ON eligible_rs.id = eligible_os.raw_signal_id
          JOIN signal_enrichments eligible_se ON eligible_se.raw_signal_id = eligible_rs.id
          WHERE eligible_os.opportunity_id = o.id
            AND eligible_os.status = 'ACTIVE'
            AND eligible_se.review_status = 'APPROVED'
            AND eligible_rs.source_type <> 'procurement'
        )"""


def _outward_postcode(postcode: str | None) -> str | None:
    if not postcode:
        return None
    value = " ".join(str(postcode).upper().split())
    return value.split(" ", 1)[0] if value else None


def _customer_title(row: dict[str, Any]) -> str:
    if row.get("customer_title"):
        return str(row["customer_title"])
    location = row.get("town") or row.get("local_authority") or row.get("region")
    subject = {
        "OPENING": "new children’s home",
        "EXPANSION": "children’s-home expansion",
        "RELOCATION": "children’s-home relocation",
        "OTHER_CHANGE": "children’s-home development",
    }.get(str(row.get("change_type")), "children’s-home development")
    operator = str(row.get("operator_name") or "").strip()
    if operator:
        return f"{operator} — {subject}{f', {location}' if location else ''}"
    return f"{subject.capitalize()}{f' — {location}' if location else ''}"


def _strength(row: dict[str, Any]) -> str:
    stage = str(row.get("lifecycle_stage") or "")
    sources = row.get("source_types") or []
    if stage in {"REGISTRATION", "OPEN"}:
        return "Confirmed"
    if len(set(sources)) >= 2:
        return "Strong"
    if stage in {"APPROVED", "FIT_OUT", "RECRUITING", "OPENING_SOON"}:
        return "Developing"
    return "Early"


def _why(row: dict[str, Any]) -> str:
    sources = set(row.get("source_types") or [])
    change = str(row.get("change_type") or "OTHER_CHANGE")
    if len(sources) >= 2:
        return "Multiple independent public sources support this opportunity."
    if "planning" in sources:
        if change == "EXPANSION":
            return "A planning application explicitly indicates increased children’s-home capacity."
        return "A planning application explicitly proposes material children’s-home provision."
    if "recruitment" in sources:
        return (
            "Recruitment evidence explicitly refers to a new or materially changing "
            "children’s home."
        )
    if "ofsted" in sources:
        return "Official Ofsted evidence confirms regulatory progress."
    return "Reviewed public evidence supports a material children’s-home change."


def _project_opportunity(row: dict[str, Any], *, saved: bool) -> dict[str, Any]:
    internal_location = row.get("location_sensitivity") == "INTERNAL_EXACT"
    postcode = _outward_postcode(row.get("postcode")) if internal_location else row.get("postcode")
    projected = {
        "id": str(row["id"]),
        "title": _customer_title(row),
        "summary": row.get("customer_summary") or _why(row),
        "operator": row.get("operator_name"),
        "town": row.get("town"),
        "local_authority": row.get("local_authority"),
        "region": row.get("region"),
        "postcode": postcode,
        "location_precision": "AREA_ONLY" if internal_location else "PUBLISHED_LOCATION",
        "change_type": row.get("change_type"),
        "change_label": CHANGE_LABELS.get(str(row.get("change_type")), "Material change"),
        "stage": row.get("lifecycle_stage"),
        "stage_label": STAGES.get(str(row.get("lifecycle_stage")), "Early signal"),
        "strength": _strength(row),
        "first_detected": row.get("first_seen_at"),
        "last_updated": row.get("latest_update_at"),
        "source_types": sorted(set(row.get("source_types") or [])),
        "why": _why(row),
        "saved": saved,
    }
    return projected


def list_customer_opportunities(
    settings: Settings,
    context: dict[str, Any],
    *,
    limit: int,
    offset: int,
    search: str | None = None,
    region: str | None = None,
    local_authority: str | None = None,
    change_type: str | None = None,
    stage: str | None = None,
    source_type: str | None = None,
    first_detected_from: str | None = None,
    updated_since: str | None = None,
    saved_only: bool = False,
) -> dict[str, Any]:
    _require_active(context)
    clauses = [_eligibility_sql()]
    params: list[Any] = []
    if not context["entitlements"]["nationwide"]:
        regions = [value.lower() for value in context["allowed_regions"]]
        authorities = [value.lower() for value in context["allowed_local_authorities"]]
        if not regions and not authorities:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        geo_clauses = []
        if regions:
            geo_clauses.append("lower(COALESCE(geo.region, '')) = ANY(%s)")
            params.append(regions)
        if authorities:
            geo_clauses.append("lower(COALESCE(geo.local_authority, '')) = ANY(%s)")
            params.append(authorities)
        clauses.append("(" + " OR ".join(geo_clauses) + ")")
    if search:
        pattern = f"%{search[:120]}%"
        clauses.append(
            "(o.name ILIKE %s OR COALESCE(o.customer_title, '') ILIKE %s "
            "OR COALESCE(o.operator_name, '') ILIKE %s "
            "OR COALESCE(o.town, '') ILIKE %s "
            "OR COALESCE(o.postcode, '') ILIKE %s "
            "OR COALESCE(geo.local_authority, '') ILIKE %s)"
        )
        params.extend([pattern] * 6)
    for value, expression in (
        (region, "lower(COALESCE(geo.region, '')) = lower(%s)"),
        (local_authority, "lower(COALESCE(geo.local_authority, '')) = lower(%s)"),
        (change_type, "o.change_type = %s"),
        (stage, "o.lifecycle_stage = %s"),
    ):
        if value:
            clauses.append(expression)
            params.append(value)
    if source_type:
        clauses.append("%s = ANY(COALESCE(geo.source_types, ARRAY[]::text[]))")
        params.append(source_type)
    if first_detected_from:
        clauses.append("o.first_seen_at >= %s::timestamptz")
        params.append(first_detected_from)
    if updated_since:
        clauses.append("o.latest_update_at >= %s::timestamptz")
        params.append(updated_since)
    if saved_only:
        clauses.append("saved.opportunity_id IS NOT NULL")
    where = " AND ".join(clauses)
    join_geo = _opportunity_geography_sql()
    join_saved = (
        "LEFT JOIN customer_saved_opportunities saved ON saved.opportunity_id = o.id "
        "AND saved.customer_user_id = %s"
    )
    base_params = [context["user_id"], *params]
    with connection(settings) as conn:
        total = conn.execute(
            f"SELECT count(*) FROM opportunities o {join_geo} {join_saved} WHERE {where}",
            base_params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT o.id, o.customer_title, o.customer_summary, o.operator_name,
                       o.postcode, o.town, o.change_type, o.lifecycle_stage,
                       o.first_seen_at, o.latest_update_at, o.location_sensitivity,
                       geo.region, geo.local_authority, geo.source_types,
                       (saved.opportunity_id IS NOT NULL) AS saved
                FROM opportunities o {join_geo} {join_saved}
                WHERE {where}
                ORDER BY o.latest_update_at DESC, o.id
                LIMIT %s OFFSET %s""",
            [*base_params, min(max(limit, 1), 50), max(offset, 0)],
        ).fetchall()
    fields = (
        "id",
        "customer_title",
        "customer_summary",
        "operator_name",
        "postcode",
        "town",
        "change_type",
        "lifecycle_stage",
        "first_seen_at",
        "latest_update_at",
        "location_sensitivity",
        "region",
        "local_authority",
        "source_types",
        "saved",
    )
    items = []
    for result in rows:
        row = dict(zip(fields, result))
        items.append(_project_opportunity(row, saved=bool(row["saved"])))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def customer_opportunity_detail(
    settings: Settings, context: dict[str, Any], opportunity_id: str
) -> dict[str, Any] | None:
    # Run an ID-scoped query through the same entitlement/eligibility rules.
    _require_active(context)
    join_geo = _opportunity_geography_sql()
    clauses = [_eligibility_sql(), "o.id = %s"]
    params: list[Any] = [opportunity_id]
    if not context["entitlements"]["nationwide"]:
        regions = [value.lower() for value in context["allowed_regions"]]
        authorities = [value.lower() for value in context["allowed_local_authorities"]]
        if not regions and not authorities:
            return None
        allowed = []
        if regions:
            allowed.append("lower(COALESCE(geo.region, '')) = ANY(%s)")
            params.append(regions)
        if authorities:
            allowed.append("lower(COALESCE(geo.local_authority, '')) = ANY(%s)")
            params.append(authorities)
        clauses.append("(" + " OR ".join(allowed) + ")")
    with connection(settings) as conn:
        row_value = conn.execute(
            f"""SELECT o.id, o.customer_title, o.customer_summary, o.operator_name,
                       o.postcode, o.town, o.change_type, o.lifecycle_stage,
                       o.first_seen_at, o.latest_update_at, o.location_sensitivity,
                       geo.region, geo.local_authority, geo.source_types,
                       EXISTS (SELECT 1 FROM customer_saved_opportunities s
                         WHERE s.customer_user_id = %s AND s.opportunity_id = o.id) AS saved,
                       o.operator_id
                FROM opportunities o {join_geo}
                WHERE {" AND ".join(clauses)}""",
            [context["user_id"], *params],
        ).fetchone()
        if not row_value:
            return None
        fields = (
            "id",
            "customer_title",
            "customer_summary",
            "operator_name",
            "postcode",
            "town",
            "change_type",
            "lifecycle_stage",
            "first_seen_at",
            "latest_update_at",
            "location_sensitivity",
            "region",
            "local_authority",
            "source_types",
            "saved",
            "operator_id",
        )
        row = dict(zip(fields, row_value))
        evidence = conn.execute(
            """SELECT rs.source_type, rs.discovered_at, rs.source_url, rs.external_id,
                      rs.title, rs.metadata
               FROM opportunity_signals os JOIN raw_signals rs ON rs.id = os.raw_signal_id
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE os.opportunity_id = %s AND os.status = 'ACTIVE'
                 AND rs.source_type <> 'procurement'
                 AND se.review_status = 'APPROVED'
               ORDER BY rs.discovered_at""",
            (opportunity_id,),
        ).fetchall()
        organisation = None
        if row.get("operator_id"):
            org = conn.execute(
                """SELECT COALESCE(legal_name, name), companies_house_number,
                          company_status, incorporation_date, companies_house_url
                   FROM operators WHERE id = %s""",
                (row["operator_id"],),
            ).fetchone()
            if org:
                organisation = {
                    "legal_name": org[0],
                    "company_number": org[1],
                    "status": org[2],
                    "incorporation_date": org[3],
                    "source_url": org[4],
                }
    projected = _project_opportunity(row, saved=bool(row["saved"]))
    projected["evidence_timeline"] = [_project_evidence(item, row) for item in evidence]
    projected["organisation"] = organisation
    projected["monitoring_message"] = (
        "CareSignal continues to monitor public planning, recruitment and regulatory "
        "evidence for meaningful updates."
    )
    return projected


def _project_evidence(item: tuple[Any, ...], opportunity: dict[str, Any]) -> dict[str, Any]:
    source_type, discovered_at, source_url, external_id, title, metadata = item
    change = str(opportunity.get("change_type") or "OTHER_CHANGE")
    descriptions = {
        "planning": "Planning evidence identified for a material children’s-home development."
        if change != "EXPANSION"
        else "Planning evidence indicates increased children’s-home capacity.",
        "recruitment": (
            "Recruitment evidence indicates staffing activity connected with this development."
        ),
        "ofsted": "Official Ofsted evidence confirms regulatory progress.",
        "companies_house": "Companies House evidence corroborates the operator’s legal identity.",
    }
    reference = None
    if isinstance(metadata, dict):
        reference = (
            metadata.get("planning_reference")
            or metadata.get("provider_application_id")
            or metadata.get("urn")
        )
    return {
        "date": discovered_at,
        "source_type": source_type,
        "source_label": {
            "planning": "Planning",
            "recruitment": "Recruitment",
            "ofsted": "Ofsted",
            "companies_house": "Companies House",
        }.get(source_type, str(source_type).title()),
        "description": descriptions.get(
            source_type, "Public evidence provides a meaningful update."
        ),
        "reference": reference or external_id,
        "source_url": source_url
        if str(source_url or "").startswith(("https://", "http://"))
        else None,
        "source_title": str(title or "")[:120] if source_type == "recruitment" else None,
    }


def save_customer_opportunity(
    settings: Settings, context: dict[str, Any], opportunity_id: str, *, saved: bool
) -> None:
    if customer_opportunity_detail(settings, context, opportunity_id) is None:
        raise ValueError("opportunity not found")
    with connection(settings) as conn:
        if saved:
            conn.execute(
                """INSERT INTO customer_saved_opportunities (customer_user_id, opportunity_id)
                   VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                (context["user_id"], opportunity_id),
            )
        else:
            conn.execute(
                """DELETE FROM customer_saved_opportunities
                   WHERE customer_user_id = %s AND opportunity_id = %s""",
                (context["user_id"], opportunity_id),
            )
        conn.commit()


def get_customer_preferences(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT frequency, regions, local_authorities, change_types, lifecycle_stages
               FROM customer_alert_preferences WHERE customer_user_id = %s""",
            (context["user_id"],),
        ).fetchone()
    if not row:
        return {
            "frequency": "WEEKLY",
            "regions": [],
            "local_authorities": [],
            "change_types": [],
            "stages": [],
        }
    return {
        "frequency": row[0],
        "regions": list(row[1] or []),
        "local_authorities": list(row[2] or []),
        "change_types": list(row[3] or []),
        "stages": list(row[4] or []),
    }


def update_customer_preferences(
    settings: Settings, context: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    frequency = str(payload.get("frequency") or "WEEKLY").upper()
    if frequency not in context["entitlements"]["alert_frequencies"]:
        raise PermissionError("alert_frequency_not_in_plan")
    values = {
        "frequency": frequency,
        "regions": _clean_list(payload.get("regions")),
        "local_authorities": _clean_list(payload.get("local_authorities")),
        "change_types": _clean_list(payload.get("change_types")),
        "stages": _clean_list(payload.get("stages")),
    }
    with connection(settings) as conn:
        conn.execute(
            """INSERT INTO customer_alert_preferences
               (customer_user_id, frequency, regions, local_authorities, change_types,
                lifecycle_stages, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (customer_user_id) DO UPDATE SET frequency = EXCLUDED.frequency,
                 regions = EXCLUDED.regions, local_authorities = EXCLUDED.local_authorities,
                 change_types = EXCLUDED.change_types,
                 lifecycle_stages = EXCLUDED.lifecycle_stages, updated_at = now()""",
            (
                context["user_id"],
                frequency,
                values["regions"],
                values["local_authorities"],
                values["change_types"],
                values["stages"],
            ),
        )
        conn.commit()
    return values


def list_saved_searches(settings: Settings, context: dict[str, Any]) -> list[dict[str, Any]]:
    if not context["entitlements"]["saved_searches"]:
        return []
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, name, criteria, digest_enabled, created_at
               FROM customer_saved_searches WHERE customer_user_id = %s
               ORDER BY created_at DESC""",
            (context["user_id"],),
        ).fetchall()
    return [
        {
            "id": row[0],
            "name": row[1],
            "criteria": row[2],
            "digest_enabled": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


def create_saved_search(
    settings: Settings, context: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    if not context["entitlements"]["saved_searches"]:
        raise PermissionError("saved_searches_not_in_plan")
    name = str(payload.get("name") or "").strip()[:100]
    criteria = payload.get("criteria") or {}
    if not name or not isinstance(criteria, dict) or len(criteria) > 20:
        raise ValueError("invalid saved search")
    allowed = {
        key: str(value)[:120]
        for key, value in criteria.items()
        if key
        in {
            "q",
            "region",
            "local_authority",
            "change_type",
            "stage",
            "source_type",
            "updated_since",
        }
        and value not in (None, "")
    }
    with connection(settings) as conn:
        row = conn.execute(
            """INSERT INTO customer_saved_searches (customer_user_id, name, criteria)
               VALUES (%s, %s, %s) RETURNING id, created_at""",
            (context["user_id"], name, Jsonb(allowed)),
        ).fetchone()
        conn.commit()
    return {
        "id": row[0],
        "name": name,
        "criteria": allowed,
        "digest_enabled": True,
        "created_at": row[1],
    }


def record_customer_event(
    settings: Settings, context: dict[str, Any], payload: dict[str, Any]
) -> None:
    event_type = str(payload.get("event_type") or "").upper()
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError("invalid event type")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict) or len(metadata) > 10:
        raise ValueError("invalid event metadata")
    safe_metadata = {
        str(key)[:50]: str(value)[:200]
        for key, value in metadata.items()
        if key in {"filter", "query", "source_type", "digest_id"}
    }
    with connection(settings) as conn:
        conn.execute(
            """INSERT INTO customer_usage_events
               (account_id, customer_user_id, event_type, opportunity_id, metadata)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                context["account_id"],
                context["user_id"],
                event_type,
                payload.get("opportunity_id"),
                Jsonb(safe_metadata),
            ),
        )
        conn.commit()


def digest_preview(
    settings: Settings,
    context: dict[str, Any],
    *,
    period_start: datetime | None = None,
) -> dict[str, Any]:
    since = (period_start or datetime.now(UTC) - timedelta(days=7)).isoformat()
    feed = list_customer_opportunities(settings, context, limit=20, offset=0, updated_since=since)
    items = feed["items"]
    portal_url = (settings.caresignal_portal_url or "").rstrip("/")
    rows = "".join(
        "<li><strong>{title}</strong><br>{stage} · {location}<br>{why}{link}</li>".format(
            title=html.escape(item["title"]),
            stage=html.escape(item["stage_label"]),
            location=html.escape(
                item.get("local_authority") or item.get("region") or "Location not published"
            ),
            why=html.escape(item["why"]),
            link=(
                f'<br><a href="{html.escape(portal_url)}/#/care/opportunities/'
                f'{html.escape(item["id"])}">View in CareSignal</a>'
                if portal_url
                else ""
            ),
        )
        for item in items
    )
    body = (
        f"<h1>CareSignal weekly update</h1><p>{len(items)} opportunities were added "
        f"or meaningfully updated in the last seven days.</p><ul>{rows}</ul>"
    )
    return {
        "subject": f"CareSignal weekly update — {len(items)} opportunities",
        "html": body,
        "opportunities": items,
        "delivery_status": "PREVIEW_ONLY" if not settings.caresignal_email_from else "READY",
    }


def provision_customer_account(
    settings: Settings, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    if not settings.cognito_user_pool_id:
        raise RuntimeError("customer identity provisioning is not configured")
    name = str(payload.get("name") or "").strip()[:160]
    email = str(payload.get("email") or "").strip().lower()[:254]
    plan = str(payload.get("plan") or "STARTER").upper()
    if (
        not name
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or plan not in PLAN_ENTITLEMENTS
    ):
        raise ValueError("invalid customer account")
    regions = _clean_list(payload.get("allowed_regions"))
    authorities = _clean_list(payload.get("allowed_local_authorities"))
    if plan == "STARTER" and not regions and not authorities:
        raise ValueError("starter accounts require an allowed geography")
    cognito = boto3.client("cognito-idp")
    response = cognito.admin_create_user(
        UserPoolId=settings.cognito_user_pool_id,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        DesiredDeliveryMediums=["EMAIL"],
    )
    attributes = {item["Name"]: item["Value"] for item in response["User"].get("Attributes", [])}
    subject = attributes.get("sub")
    if not subject:
        raise RuntimeError("Cognito did not return a user subject")
    cognito.admin_add_user_to_group(
        UserPoolId=settings.cognito_user_pool_id, Username=email, GroupName=settings.customer_group
    )
    try:
        with connection(settings) as conn:
            account = conn.execute(
                """INSERT INTO customer_accounts
                   (name, status, plan, allowed_regions, allowed_local_authorities, created_by)
                   VALUES (%s, 'PILOT', %s, %s, %s, %s) RETURNING id, created_at""",
                (name, plan, regions, authorities, actor),
            ).fetchone()
            user = conn.execute(
                """INSERT INTO customer_users
                   (account_id, cognito_sub, email, role) VALUES (%s, %s, %s, 'OWNER')
                   RETURNING id""",
                (account[0], subject, email),
            ).fetchone()
            conn.execute(
                """INSERT INTO customer_alert_preferences (customer_user_id, frequency)
                   VALUES (%s, 'WEEKLY')""",
                (user[0],),
            )
            conn.execute(
                """INSERT INTO admin_audit_events (action, actor, target_type, details)
                   VALUES ('customer_account_provisioned', %s, 'customer_account', %s)""",
                (
                    actor,
                    Jsonb(
                        {
                            "account_id": str(account[0]),
                            "email": email,
                            "plan": plan,
                            "allowed_regions": regions,
                            "allowed_local_authorities": authorities,
                        }
                    ),
                ),
            )
            conn.commit()
    except Exception:
        cognito.admin_delete_user(UserPoolId=settings.cognito_user_pool_id, Username=email)
        raise
    return {
        "id": account[0],
        "name": name,
        "status": "PILOT",
        "plan": plan,
        "owner_email": email,
        "created_at": account[1],
    }


def list_customer_accounts(settings: Settings) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT a.id, a.name, a.status, a.plan, a.allowed_regions,
                      a.allowed_local_authorities, a.created_at,
                      count(DISTINCT u.id), count(DISTINCT e.id)
               FROM customer_accounts a
               LEFT JOIN customer_users u ON u.account_id = a.id
               LEFT JOIN customer_usage_events e ON e.account_id = a.id
               GROUP BY a.id ORDER BY a.created_at DESC"""
        ).fetchall()
    fields = (
        "id",
        "name",
        "status",
        "plan",
        "allowed_regions",
        "allowed_local_authorities",
        "created_at",
        "user_count",
        "usage_events",
    )
    return [dict(zip(fields, row)) for row in rows]


def update_customer_account(
    settings: Settings, account_id: str, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    status = str(payload.get("status") or "").upper() or None
    plan = str(payload.get("plan") or "").upper() or None
    if status and status not in {"PILOT", "ACTIVE", "SUSPENDED"}:
        raise ValueError("invalid account status")
    if plan and plan not in PLAN_ENTITLEMENTS:
        raise ValueError("invalid plan")
    regions = _clean_list(payload.get("allowed_regions")) if "allowed_regions" in payload else None
    authorities = (
        _clean_list(payload.get("allowed_local_authorities"))
        if "allowed_local_authorities" in payload
        else None
    )
    with connection(settings) as conn:
        current = conn.execute(
            "SELECT status, plan, allowed_regions, allowed_local_authorities "
            "FROM customer_accounts WHERE id = %s FOR UPDATE",
            (account_id,),
        ).fetchone()
        if not current:
            raise ValueError("customer account not found")
        values = (
            status or current[0],
            plan or current[1],
            regions if regions is not None else current[2],
            authorities if authorities is not None else current[3],
        )
        if values[1] == "STARTER" and not values[2] and not values[3]:
            raise ValueError("starter accounts require an allowed geography")
        conn.execute(
            "UPDATE customer_accounts SET status = %s, plan = %s, "
            "allowed_regions = %s, allowed_local_authorities = %s, "
            "updated_at = now() WHERE id = %s",
            (*values, account_id),
        )
        conn.execute(
            "INSERT INTO admin_audit_events (action, actor, target_type, details) "
            "VALUES ('customer_account_updated', %s, 'customer_account', %s)",
            (actor, Jsonb({"account_id": account_id, "status": values[0], "plan": values[1]})),
        )
        conn.commit()
    return {
        "id": account_id,
        "status": values[0],
        "plan": values[1],
        "allowed_regions": values[2],
        "allowed_local_authorities": values[3],
    }


def set_opportunity_publication(
    settings: Settings, opportunity_id: str, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    status = str(payload.get("status") or "").upper()
    if status not in {"DRAFT", "PUBLISHED", "WITHDRAWN"}:
        raise ValueError("invalid publication status")
    title = str(payload.get("customer_title") or "").strip()[:160] or None
    summary = str(payload.get("customer_summary") or "").strip()[:500] or None
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT o.vertical,
               EXISTS (SELECT 1 FROM opportunity_signals os
                 JOIN raw_signals rs ON rs.id = os.raw_signal_id
                 JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                 WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                   AND se.review_status = 'APPROVED' AND rs.source_type <> 'procurement')
               FROM opportunities o WHERE o.id = %s FOR UPDATE""",
            (opportunity_id,),
        ).fetchone()
        if not row or row[0] != "CHILDRENS_HOME":
            raise ValueError("CareSignal opportunity not found")
        if status == "PUBLISHED" and not row[1]:
            raise ValueError("opportunity requires approved non-procurement evidence")
        conn.execute(
            """UPDATE opportunities SET publication_status = %s,
                 customer_title = COALESCE(%s, customer_title),
                 customer_summary = COALESCE(%s, customer_summary),
                 customer_published_by = CASE WHEN %s = 'PUBLISHED'
                   THEN %s ELSE customer_published_by END,
                 customer_published_at = CASE WHEN %s = 'PUBLISHED'
                   THEN now() ELSE customer_published_at END,
                 updated_at = now() WHERE id = %s""",
            (status, title, summary, status, actor, status, opportunity_id),
        )
        conn.execute(
            "INSERT INTO admin_audit_events "
            "(action, actor, target_type, details, vertical) VALUES "
            "('customer_publication_changed', %s, 'opportunity', %s, "
            "'CHILDRENS_HOME')",
            (actor, Jsonb({"opportunity_id": opportunity_id, "status": status})),
        )
        conn.commit()
    return {
        "id": opportunity_id,
        "publication_status": status,
        "customer_title": title,
        "customer_summary": summary,
    }


def customer_readiness(settings: Settings) -> dict[str, Any]:
    with connection(settings) as conn:
        row = conn.execute(
            f"""SELECT
                 count(*) FILTER (WHERE {_eligibility_sql()}),
                 count(*) FILTER (WHERE o.vertical = 'CHILDRENS_HOME'
                   AND o.publication_status = 'DRAFT'
                   AND o.review_status NOT IN ('REJECTED', 'MERGED')),
                 count(*) FILTER (WHERE o.vertical = 'CHILDRENS_HOME'
                   AND o.publication_status = 'PUBLISHED')
               FROM opportunities o"""
        ).fetchone()
        geography = conn.execute(
            """SELECT count(DISTINCT town), count(DISTINCT postcode)
               FROM opportunities WHERE vertical = 'CHILDRENS_HOME'
                 AND publication_status = 'PUBLISHED'"""
        ).fetchone()
    return {
        "customer_eligible": row[0],
        "draft_candidates": row[1],
        "published_total": row[2],
        "published_towns": geography[0],
        "published_postcodes": geography[1],
        "email_delivery_configured": bool(settings.caresignal_email_from),
    }


def pilot_curation_inventory(settings: Settings, *, limit: int = 100) -> dict[str, Any]:
    """Return a bounded internal-only inventory for the paid-pilot quality gate.

    This deliberately returns source summaries rather than raw evidence documents. It is invoked
    through the IAM-protected Lambda operational path, not exposed as a customer or admin HTTP API.
    """
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        opportunity_rows = conn.execute(
            """SELECT o.id, o.name, o.customer_title, o.customer_summary,
                      o.operator_name, o.town, o.postcode, o.change_type,
                      o.lifecycle_stage, o.confidence, o.review_status,
                      o.publication_status, o.first_seen_at, o.latest_update_at,
                      o.location_sensitivity, o.creation_reason, o.stage_reason,
                      o.created_at
               FROM opportunities o
               WHERE o.vertical = 'CHILDRENS_HOME'
                 AND o.review_status NOT IN ('REJECTED', 'MERGED')
                 AND o.merged_into_opportunity_id IS NULL
               ORDER BY o.latest_update_at DESC, o.id
               LIMIT %s""",
            (bounded_limit,),
        ).fetchall()
        evidence_rows = conn.execute(
            """SELECT os.opportunity_id, rs.id, rs.source_type, rs.discovered_at,
                      rs.title, rs.source_url, rs.external_id, rs.metadata,
                      se.review_status, se.confidence, se.extracted_facts,
                      os.created_by, os.match_reason
               FROM opportunity_signals os
               JOIN raw_signals rs ON rs.id = os.raw_signal_id
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE os.status = 'ACTIVE'
                 AND os.opportunity_id = ANY(%s::uuid[])
               ORDER BY os.opportunity_id, rs.discovered_at, rs.id""",
            ([str(row[0]) for row in opportunity_rows],),
        ).fetchall() if opportunity_rows else []

    evidence_by_opportunity: dict[str, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        opportunity_id = str(row[0])
        metadata = row[7] if isinstance(row[7], dict) else {}
        evidence_by_opportunity.setdefault(opportunity_id, []).append(
            {
                "signal_id": str(row[1]),
                "source_type": row[2],
                "discovered_at": row[3].isoformat() if row[3] else None,
                "title": str(row[4] or "")[:500],
                "source_url": row[5]
                if str(row[5] or "").startswith(("https://", "http://"))
                else None,
                "external_id": row[6],
                "region": metadata.get("region") or metadata.get("provider_region"),
                "local_authority": (
                    metadata.get("local_authority")
                    or metadata.get("council")
                    or (metadata.get("authority") or {}).get("name")
                    if isinstance(metadata.get("authority"), dict)
                    else metadata.get("local_authority") or metadata.get("council")
                ),
                "review_status": row[8],
                "rule_confidence": float(row[9]) if row[9] is not None else None,
                "event_type": (row[10] or {}).get("event_type")
                if isinstance(row[10], dict)
                else None,
                "linked_by": row[11],
                "match_reason": row[12],
            }
        )

    items = []
    for row in opportunity_rows:
        opportunity_id = str(row[0])
        evidence = evidence_by_opportunity.get(opportunity_id, [])
        approved = [
            item
            for item in evidence
            if item["review_status"] == "APPROVED" and item["source_type"] != "procurement"
        ]
        source_types = sorted({item["source_type"] for item in approved})
        regions = sorted({item["region"] for item in evidence if item.get("region")})
        authorities = sorted(
            {item["local_authority"] for item in evidence if item.get("local_authority")}
        )
        row_map = {
            "id": opportunity_id,
            "name": row[1],
            "customer_title": row[2],
            "customer_summary": row[3],
            "operator_name": row[4],
            "town": row[5],
            "postcode": row[6],
            "change_type": row[7],
            "lifecycle_stage": row[8],
            "confidence": float(row[9]) if row[9] is not None else None,
            "review_status": row[10],
            "publication_status": row[11],
            "first_seen_at": row[12],
            "latest_update_at": row[13],
            "location_sensitivity": row[14],
            "creation_reason": row[15],
            "stage_reason": row[16],
        }
        projected = _project_opportunity(
            {
                **row_map,
                "region": regions[0] if len(regions) == 1 else None,
                "local_authority": authorities[0] if len(authorities) == 1 else None,
                "source_types": source_types,
            },
            saved=False,
        )
        issues = []
        if not approved:
            issues.append("no approved non-procurement evidence")
        if not (row[5] or row[6] or regions or authorities):
            issues.append("unclear geography")
        if not source_types:
            issues.append("no customer-safe evidence source")
        if not all(item.get("source_url") for item in approved):
            issues.append("one or more official source links missing")
        items.append(
            {
                "id": opportunity_id,
                "internal_name": row[1],
                "publication_status": row[11],
                "review_status": row[10],
                "customer_preview": {
                    **projected,
                    "first_detected": row[12].isoformat() if row[12] else None,
                    "last_updated": row[13].isoformat() if row[13] else None,
                },
                "creation_reason": row[15],
                "stage_reason": row[16],
                "approved_source_types": source_types,
                "evidence": evidence,
                "quality_issues": issues,
                "eligible_for_publication": not issues,
            }
        )
    return {"count": len(items), "limit": bounded_limit, "items": items}


def apply_pilot_publications(
    settings: Settings, publications: Any, *, actor: str
) -> dict[str, Any]:
    """Apply an explicit, bounded operational publication list with normal audit semantics."""
    if not isinstance(publications, list) or not publications or len(publications) > 30:
        raise ValueError("pilot publications must contain 1 to 30 explicit items")
    seen: set[str] = set()
    for value in publications:
        if not isinstance(value, dict):
            raise ValueError("invalid pilot publication")
        opportunity_id = str(value.get("id") or "")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", opportunity_id):
            raise ValueError("invalid pilot publication")
        if opportunity_id in seen:
            raise ValueError("duplicate pilot publication")
        seen.add(opportunity_id)
    results = []
    for value in publications:
        opportunity_id = str(value["id"])
        result = set_opportunity_publication(
            settings,
            opportunity_id,
            {
                "status": "PUBLISHED",
                "customer_title": value.get("customer_title"),
                "customer_summary": value.get("customer_summary"),
            },
            actor,
        )
        results.append(result)
    return {"published": len(results), "items": results}
