from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from psycopg.types.json import Jsonb

from app.care_lifecycle import derive_care_lifecycle
from app.config import Settings
from app.correlation import normalize_identity
from app.customer_projection import (
    customer_safe_location,
    customer_safe_place,
    customer_summary,
    customer_title,
    generated_customer_summary,
    postcode_district,
    safe_evidence_title,
)
from app.db import connection
from app.evidence_support import (
    EvidenceSupport,
    classify_evidence_support,
    current_opportunity_basis_reason,
)
from app.nursery_customer_publication import (
    ELIGIBLE as NURSERY_CUSTOMER_PUBLICATION_ELIGIBLE,
)
from app.nursery_customer_publication import (
    POLICY_VERSION as NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION,
)
from app.nursery_customer_publication import (
    evaluate_nursery_customer_publication,
)
from app.nursery_customer_publication import (
    preview_summary as nursery_customer_publication_preview_summary,
)
from app.official_planning_fetch import fetch_idox_party_via_fetcher
from app.organisation_types import is_public_authority_name
from app.planning_outcomes import canonical_planning_outcome
from app.planning_parties import extract_planning_party_provenance
from app.planning_party_official import official_party_provenance
from app.verticals import CHILDRENS_HOME, NURSERY, validate_vertical

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
    "PLANNING_PENDING": "Planning pending",
    "PLANNING_APPROVED": "Planning approved",
    "DELIVERY_SIGNAL_DETECTED": "Delivery signal detected",
    "REGISTRATION_DETECTED": "Registration detected",
    "REGISTERED": "Registered",
    "APPEAL_PENDING": "Planning appeal pending",
    "STOPPED": "Stopped",
    "NEEDS_REVIEW": "Status under review",
}
CHANGE_LABELS = {
    "OPENING": "New opening",
    "EXPANSION": "Expansion",
    "RELOCATION": "Relocation",
    "OTHER_CHANGE": "Material change",
}
NURSERY_STAGES = {
    "PLANNING_PENDING": "Planning pending",
    "PLANNING_APPROVED": "Planning approved",
    "RECRUITMENT_ACTIVITY": "Recruitment activity",
    "OTHER_CHANGE": "Other nursery change",
    "UNDER_REVIEW": "Under review",
}
NURSERY_CHANGE_LABELS = {
    "OPENING": "New nursery",
    "EXPANSION": "Nursery expansion",
    "RELOCATION": "Nursery relocation",
    "OTHER_CHANGE": "Other nursery change",
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
                      a.allowed_local_authorities,
                      COALESCE((SELECT array_agg(cav.vertical ORDER BY cav.vertical)
                        FROM customer_account_verticals cav WHERE cav.account_id = a.id),
                        ARRAY['CHILDRENS_HOME']::text[])
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
        "allowed_verticals": list(row[11] or [CHILDRENS_HOME]),
        "entitlements": PLAN_ENTITLEMENTS[plan],
    }


def _require_active(context: dict[str, Any]) -> None:
    if context["user_status"] != "ACTIVE" or context["account_status"] == "SUSPENDED":
        raise PermissionError("customer_account_suspended")


def _opportunity_geography_sql() -> str:
    return """LEFT JOIN LATERAL (
        SELECT
          (array_agg(NULLIF(rs.metadata->>'region', '')) FILTER
              (WHERE rs.metadata->>'region' IS NOT NULL))[1]
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


def _allowed_customer_vertical(context: dict[str, Any], value: str | None) -> str:
    vertical = validate_vertical(value or CHILDRENS_HOME)
    if vertical not in set(context.get("allowed_verticals") or [CHILDRENS_HOME]):
        raise PermissionError("customer_vertical_not_permitted")
    return vertical


def _eligibility_sql(vertical: str = CHILDRENS_HOME) -> str:
    return f"""o.vertical = '{validate_vertical(vertical)}'
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
    return postcode_district(postcode)


def _customer_title(row: dict[str, Any]) -> str:
    return customer_title(row)


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
    return generated_customer_summary(row)


def _project_opportunity(row: dict[str, Any], *, saved: bool) -> dict[str, Any]:
    postcode = _outward_postcode(row.get("postcode"))
    town = customer_safe_place(row.get("town"))
    local_authority = customer_safe_place(row.get("local_authority"), authority=True)
    region = customer_safe_place(row.get("region"))
    projected = {
        "id": str(row["id"]),
        "title": _customer_title(row),
        "summary": customer_summary(row),
        "operator": row.get("operator_name"),
        "town": town,
        "local_authority": local_authority,
        "region": region,
        "postcode": postcode,
        "location_precision": "AREA_ONLY",
        "vertical": row.get("vertical") or CHILDRENS_HOME,
        "change_type": row.get("change_type"),
        "change_label": (
            NURSERY_CHANGE_LABELS.get(str(row.get("change_type")), "Nursery opportunity")
            if row.get("vertical") == NURSERY
            else CHANGE_LABELS.get(str(row.get("change_type")), "Material change")
        ),
        "stage": row.get("lifecycle_stage"),
        "stage_label": (
            NURSERY_STAGES.get(str(row.get("lifecycle_stage")), "Under review")
            if row.get("vertical") == NURSERY
            else STAGES.get(str(row.get("lifecycle_stage")), "Early signal")
        ),
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
    vertical: str | None = None,
) -> dict[str, Any]:
    _require_active(context)
    selected_vertical = _allowed_customer_vertical(context, vertical)
    clauses = [_eligibility_sql(selected_vertical)]
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
        (stage, "COALESCE(o.customer_lifecycle_stage, o.lifecycle_stage) = %s"),
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
                       o.postcode, o.town, o.change_type, o.vertical,
                       COALESCE(o.customer_lifecycle_stage, o.lifecycle_stage),
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
        "vertical",
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
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "vertical": selected_vertical,
    }


def customer_opportunity_detail(
    settings: Settings, context: dict[str, Any], opportunity_id: str, *, vertical: str | None = None
) -> dict[str, Any] | None:
    # Run an ID-scoped query through the same entitlement/eligibility rules.
    _require_active(context)
    join_geo = _opportunity_geography_sql()
    selected_vertical = _allowed_customer_vertical(context, vertical)
    clauses = [_eligibility_sql(selected_vertical), "o.id = %s"]
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
                       o.postcode, o.town, o.change_type, o.vertical,
                       COALESCE(o.customer_lifecycle_stage, o.lifecycle_stage),
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
            "vertical",
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
    product = "NurserySignal" if row.get("vertical") == NURSERY else "CareProspect"
    projected["monitoring_message"] = (
        f"{product} continues to monitor reviewed public evidence for meaningful updates."
    )
    return projected


def _project_evidence(item: tuple[Any, ...], opportunity: dict[str, Any]) -> dict[str, Any]:
    source_type, discovered_at, source_url, external_id, title, metadata = item
    change = str(opportunity.get("change_type") or "OTHER_CHANGE")
    nursery = opportunity.get("vertical") == NURSERY
    subject = "nursery" if nursery else "children’s-home"
    descriptions = {
        "planning": f"Planning evidence identified for a material {subject} development."
        if change != "EXPANSION"
        else f"Planning evidence indicates increased {subject} capacity.",
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
        "source_title": safe_evidence_title(title) if source_type == "recruitment" else None,
    }


def save_customer_opportunity(
    settings: Settings,
    context: dict[str, Any],
    opportunity_id: str,
    *,
    saved: bool,
    vertical: str | None = None,
) -> None:
    if customer_opportunity_detail(settings, context, opportunity_id, vertical=vertical) is None:
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


def list_saved_searches(
    settings: Settings, context: dict[str, Any], *, vertical: str | None = None
) -> list[dict[str, Any]]:
    if not context["entitlements"]["saved_searches"]:
        return []
    selected_vertical = _allowed_customer_vertical(context, vertical)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, name, criteria, digest_enabled, created_at
               FROM customer_saved_searches WHERE customer_user_id = %s
                 AND COALESCE(criteria->>'vertical', 'CHILDRENS_HOME') = %s
               ORDER BY created_at DESC""",
            (context["user_id"], selected_vertical),
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
    selected_vertical = _allowed_customer_vertical(context, criteria.get("vertical"))
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
    allowed["vertical"] = selected_vertical
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
    vertical: str | None = None,
) -> dict[str, Any]:
    since = (period_start or datetime.now(UTC) - timedelta(days=7)).isoformat()
    selected_vertical = _allowed_customer_vertical(context, vertical)
    feed = list_customer_opportunities(
        settings, context, limit=20, offset=0, updated_since=since, vertical=selected_vertical
    )
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
                f'{html.escape(item["id"])}">View in CareProspect</a>'
                if portal_url
                else ""
            ),
        )
        for item in items
    )
    product = "NurserySignal" if selected_vertical == NURSERY else "CareProspect"
    preferences_url = (
        f"{portal_url}/#/care/alerts?vertical={selected_vertical}" if portal_url else ""
    )
    body = (
        '<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto">'
        f'<h1 style="color:#193d35">{product} weekly update</h1>'
        f"<p>{len(items)} opportunities were added or meaningfully updated in the "
        f"last seven days.</p><ul>{rows}</ul>"
        + (
            f'<p style="color:#5d6b66;font-size:13px">Manage or turn off alerts in '
            f'<a href="{html.escape(preferences_url)}">{product} preferences</a>.</p>'
            if preferences_url
            else ""
        )
        + "</div>"
    )
    return {
        "subject": f"{product} weekly update — {len(items)} opportunities",
        "html": body,
        "opportunities": items,
        "delivery_status": "PREVIEW_ONLY" if not settings.caresignal_email_from else "READY",
        "vertical": selected_vertical,
    }


def _validated_customer_account(payload: dict[str, Any]) -> dict[str, Any]:
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
    requested_verticals = payload.get("allowed_verticals") or [CHILDRENS_HOME]
    if not isinstance(requested_verticals, list) or not requested_verticals:
        raise ValueError("invalid allowed verticals")
    verticals = sorted({validate_vertical(str(value)) for value in requested_verticals})
    if plan == "STARTER" and not regions and not authorities:
        raise ValueError("starter accounts require an allowed geography")
    return {
        "name": name,
        "email": email,
        "plan": plan,
        "allowed_regions": regions,
        "allowed_local_authorities": authorities,
        "allowed_verticals": verticals,
    }


def queue_customer_account_provision(
    settings: Settings, payload: dict[str, Any], actor: str
) -> dict[str, Any]:
    if not settings.customer_provisioning_queue_url:
        raise RuntimeError("customer identity provisioning is not configured")
    request = _validated_customer_account(payload)
    request["actor"] = actor[:200]
    boto3.client("sqs").send_message(
        QueueUrl=settings.customer_provisioning_queue_url,
        MessageBody=json.dumps(request, separators=(",", ":")),
    )
    return {"status": "QUEUED", "name": request["name"], "owner_email": request["email"]}


def record_customer_account(
    settings: Settings, payload: dict[str, Any], actor: str, cognito_sub: str
) -> dict[str, Any]:
    request = _validated_customer_account(payload)
    subject = str(cognito_sub or "").strip()[:200]
    if not subject:
        raise ValueError("missing customer identity subject")
    with connection(settings) as conn:
        existing = conn.execute(
            """SELECT a.id, a.name, a.status, a.plan, u.email, a.created_at, u.cognito_sub
               FROM customer_users u JOIN customer_accounts a ON a.id = u.account_id
               WHERE u.cognito_sub = %s OR lower(u.email) = %s
               ORDER BY a.created_at LIMIT 1 FOR UPDATE""",
            (subject, request["email"]),
        ).fetchone()
        if existing:
            if existing[6] != subject:
                raise ValueError("customer email belongs to another identity")
            return {
                "id": str(existing[0]),
                "name": existing[1],
                "status": existing[2],
                "plan": existing[3],
                "owner_email": existing[4],
                "created_at": existing[5].isoformat(),
                "idempotent": True,
            }
        account = conn.execute(
            """INSERT INTO customer_accounts
               (name, status, plan, allowed_regions, allowed_local_authorities, created_by)
               VALUES (%s, 'PILOT', %s, %s, %s, %s) RETURNING id, created_at""",
            (
                request["name"],
                request["plan"],
                request["allowed_regions"],
                request["allowed_local_authorities"],
                actor,
            ),
        ).fetchone()
        user = conn.execute(
            """INSERT INTO customer_users
               (account_id, cognito_sub, email, role) VALUES (%s, %s, %s, 'OWNER')
               RETURNING id""",
            (account[0], subject, request["email"]),
        ).fetchone()
        for vertical in request["allowed_verticals"]:
            conn.execute(
                """INSERT INTO customer_account_verticals (account_id, vertical, granted_by)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (account[0], vertical, actor),
            )
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
                        "email": request["email"],
                        "plan": request["plan"],
                        "allowed_regions": request["allowed_regions"],
                        "allowed_local_authorities": request["allowed_local_authorities"],
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "id": str(account[0]),
        "name": request["name"],
        "status": "PILOT",
        "plan": request["plan"],
        "owner_email": request["email"],
        "created_at": account[1].isoformat(),
        "idempotent": False,
    }


def list_customer_accounts(settings: Settings) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT a.id, a.name, a.status, a.plan, a.allowed_regions,
                      a.allowed_local_authorities, a.created_at,
                      COALESCE(array_agg(DISTINCT cav.vertical)
                        FILTER (WHERE cav.vertical IS NOT NULL),
                        ARRAY['CHILDRENS_HOME']::text[]),
                      count(DISTINCT u.id), count(DISTINCT e.id)
               FROM customer_accounts a
               LEFT JOIN customer_users u ON u.account_id = a.id
               LEFT JOIN customer_usage_events e ON e.account_id = a.id
               LEFT JOIN customer_account_verticals cav ON cav.account_id = a.id
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
        "allowed_verticals",
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
    requested_verticals = (
        payload.get("allowed_verticals") if "allowed_verticals" in payload else None
    )
    if requested_verticals is not None and (
        not isinstance(requested_verticals, list) or not requested_verticals
    ):
        raise ValueError("invalid allowed verticals")
    verticals = (
        sorted({validate_vertical(str(value)) for value in requested_verticals})
        if requested_verticals is not None
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
        if verticals is not None:
            conn.execute(
                "DELETE FROM customer_account_verticals WHERE account_id = %s", (account_id,)
            )
            for vertical in verticals:
                conn.execute(
                    """INSERT INTO customer_account_verticals (account_id, vertical, granted_by)
                       VALUES (%s, %s, %s)""",
                    (account_id, vertical, actor),
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
        "allowed_verticals": verticals,
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
            raise ValueError("CareProspect opportunity not found")
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


def _nursery_customer_publication_rows(conn: Any) -> list[dict[str, Any]]:
    """Fetch only the local, active evidence needed for the v1 preview."""
    rows = conn.execute(
        """SELECT o.id, o.name, o.vertical, o.publication_status, o.review_status,
                  o.merged_into_opportunity_id, o.change_type, o.lifecycle_stage,
                  o.town, o.postcode, o.operator_name, o.first_seen_at, o.latest_update_at,
                  COALESCE(array_agg(DISTINCT rs.source_type) FILTER (WHERE os.status = 'ACTIVE'),
                    ARRAY[]::text[]) AS source_types,
                  count(DISTINCT rs.id) FILTER (WHERE os.status = 'ACTIVE'
                    AND rs.source_type = 'planning' AND se.review_status = 'APPROVED')
                    AS approved_planning_count,
                  (array_agg(se.extracted_facts ORDER BY rs.discovered_at DESC)
                    FILTER (WHERE os.status = 'ACTIVE' AND rs.source_type = 'planning'
                      AND se.review_status = 'APPROVED'))[1] AS facts,
                  (array_agg(rs.metadata ORDER BY rs.discovered_at DESC)
                    FILTER (WHERE os.status = 'ACTIVE' AND rs.source_type = 'planning'
                      AND se.review_status = 'APPROVED'))[1] AS planning_metadata,
                  (array_agg(rs.title ORDER BY rs.discovered_at DESC)
                    FILTER (WHERE os.status = 'ACTIVE' AND rs.source_type = 'planning'
                      AND se.review_status = 'APPROVED'))[1] AS signal_title
           FROM opportunities o
           LEFT JOIN opportunity_signals os ON os.opportunity_id = o.id
           LEFT JOIN raw_signals rs ON rs.id = os.raw_signal_id
           LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
           WHERE o.vertical = 'NURSERY'
           GROUP BY o.id
           ORDER BY o.latest_update_at DESC, o.id"""
    ).fetchall()
    fields = (
        "id",
        "name",
        "vertical",
        "publication_status",
        "review_status",
        "merged_into_opportunity_id",
        "change_type",
        "lifecycle_stage",
        "town",
        "postcode",
        "operator_name",
        "first_seen_at",
        "latest_update_at",
        "source_types",
        "approved_planning_count",
        "facts",
        "planning_metadata",
        "signal_title",
    )
    result: list[dict[str, Any]] = []
    for values in rows:
        item = dict(zip(fields, values))
        item["facts"] = item.get("facts") or {}
        item["source_types"] = list(item.get("source_types") or [])
        item["site_identity"] = bool(
            customer_safe_location(town=item.get("town"), postcode=item.get("postcode"))
        )
        metadata = item.get("planning_metadata") or {}
        item["planning_outcome"] = canonical_planning_outcome(metadata).outcome.value
        item["policy"] = evaluate_nursery_customer_publication(item)
        result.append(item)
    return result


def nursery_customer_publication_preview(settings: Settings, *, limit: int = 25) -> dict[str, Any]:
    """Read-only first-cohort report. It never contacts a provider."""
    with connection(settings) as conn:
        rows = _nursery_customer_publication_rows(conn)
    summary = nursery_customer_publication_preview_summary(rows)
    eligible = [
        item for item in rows if item["policy"]["outcome"] == NURSERY_CUSTOMER_PUBLICATION_ELIGIBLE
    ]
    samples = []
    for item in rows[: min(max(limit, 1), 50)]:
        samples.append(
            {
                "opportunity_id": str(item["id"]),
                "name": item["name"],
                "change_type": item["change_type"],
                "planning_outcome": item["planning_outcome"],
                "site_identity": item["site_identity"],
                "operator_known": bool(item.get("operator_name")),
                "policy": item["policy"],
                "customer_preview": _project_opportunity(
                    {**item, "lifecycle_stage": item["policy"]["stage"]}, saved=False
                ),
            }
        )
    return {
        "schema_version": "signalhub-nursery-customer-publication-preview-v1",
        "policy_version": NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION,
        "preview_only": True,
        "provider_calls": 0,
        "total_active_nursery_opportunities": len(rows),
        "eligible_count": len(eligible),
        "summary": summary,
        "samples": samples,
    }


def apply_nursery_customer_publications(
    settings: Settings, *, limit: int, actor: str
) -> dict[str, Any]:
    """Publish at most 25 freshly re-evaluated, v1-eligible Nursery records."""
    bounded = min(max(int(limit), 1), 25)
    with connection(settings) as conn:
        run = conn.execute(
            """INSERT INTO nursery_customer_publication_runs
               (policy_version, actor, requested_limit) VALUES (%s, %s, %s) RETURNING id""",
            (NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION, actor, bounded),
        ).fetchone()[0]
        rows = _nursery_customer_publication_rows(conn)
        selected = [
            item
            for item in rows
            if item["policy"]["outcome"] == NURSERY_CUSTOMER_PUBLICATION_ELIGIBLE
        ][:bounded]
        published = skipped = failed = 0
        for item in selected:
            try:
                # A lock plus fresh local evaluation prevents a preview from becoming authority.
                locked = conn.execute(
                    "SELECT publication_status FROM opportunities WHERE id = %s FOR UPDATE",
                    (item["id"],),
                ).fetchone()
                if not locked or locked[0] != "DRAFT":
                    status, reason = "SKIPPED", "not_draft_at_mutation"
                    skipped += 1
                else:
                    conn.execute(
                        """UPDATE opportunities SET publication_status = 'PUBLISHED',
                           customer_lifecycle_stage = %s,
                           customer_lifecycle_reason = 'Derived for Nursery customer publication',
                           customer_lifecycle_policy_version = %s,
                           customer_lifecycle_evaluated_at = now(),
                           customer_published_by = %s,
                           customer_published_at = now(), updated_at = now()
                           WHERE id = %s""",
                        (
                            item["policy"]["stage"],
                            NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION,
                            f"AUTOMATION:{NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION}",
                            item["id"],
                        ),
                    )
                    conn.execute(
                        """INSERT INTO admin_audit_events
                           (action, actor, target_type, details, vertical)
                           VALUES (
                             'nursery_customer_published', %s, 'opportunity', %s, 'NURSERY'
                           )""",
                        (
                            actor,
                            Jsonb(
                                {
                                    "opportunity_id": str(item["id"]),
                                    "policy_version": NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION,
                                    "reason": item["policy"]["reasons"][0],
                                }
                            ),
                        ),
                    )
                    status, reason = "PUBLISHED", item["policy"]["reasons"][0]
                    published += 1
            except Exception as exc:  # per-record isolation, details stay non-sensitive
                status, reason = "FAILED", type(exc).__name__
                failed += 1
            conn.execute(
                """INSERT INTO nursery_customer_publication_run_items
                   (run_id, opportunity_id, status, reason, details)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    run,
                    item["id"],
                    status,
                    reason,
                    Jsonb({"policy_version": NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION}),
                ),
            )
        conn.execute(
            """UPDATE nursery_customer_publication_runs SET selected_count = %s,
               published_count = %s, skipped_count = %s, failed_count = %s, completed_at = now()
               WHERE id = %s""",
            (len(selected), published, skipped, failed, run),
        )
        conn.commit()
    return {
        "run_id": str(run),
        "selected": len(selected),
        "published": published,
        "skipped": skipped,
        "failed": failed,
        "policy_version": NURSERY_CUSTOMER_PUBLICATION_POLICY_VERSION,
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


CUSTOMER_PUBLICATION_QUALITY_SCHEMA_VERSION = "signalhub-customer-publication-quality-v1"
CUSTOMER_OPERATOR_ENRICHMENT_POLICY_VERSION = "care-customer-operator-enrichment-v1"
CUSTOMER_NEEDS_REVIEW_WORDING_POLICY_VERSION = "care-customer-needs-review-wording-v1"


def _company_like_applicant(value: Any) -> bool:
    """Return whether a Planning applicant is safe to treat as an organisation candidate.

    This deliberately does *not* try to infer an eventual operator from a person's
    name, an agent, or a council.  Exact matching is only useful after this role
    guard has established that the value looks like a legal/organisational applicant.
    """
    name = " ".join(str(value or "").split()).strip(" .,")
    if not name or is_public_authority_name(name):
        return False
    normalized = normalize_identity(name)
    if normalized in {"unknown", "not provided", "redacted", "applicant", "n a"}:
        return False
    return bool(
        re.search(
            r"\b(?:ltd|limited|plc|llp|cic|inc|company|co|group|holdings|trust|"
            r"foundation|association|partnership)\b",
            name,
            re.IGNORECASE,
        )
    )


def classify_customer_operator_identity(
    *,
    applicants: list[str],
    agents: list[str],
    organisations_by_identity: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    """Classify local Planning applicant evidence without creating organisations.

    The function is intentionally pure so preview, apply and regression tests share
    the exact same conservative rule.  Only an exact applicant-to-existing-local
    organisation identity can be ``SAFE_AUTO_LINK``; agents are never link inputs.
    """
    clean_applicants = list(dict.fromkeys(v.strip() for v in applicants if str(v).strip()))
    clean_agents = list(dict.fromkeys(v.strip() for v in agents if str(v).strip()))
    company_applicants = [value for value in clean_applicants if _company_like_applicant(value)]
    unsafe_applicants = [value for value in clean_applicants if is_public_authority_name(value)]
    if unsafe_applicants:
        return {
            "outcome": "UNSAFE_TO_USE",
            "reason": "planning applicant is a public authority, not operator evidence",
            "applicant_evidence": clean_applicants,
            "agent_evidence": clean_agents,
            "proposed_organisation": None,
            "automation_allowed": False,
            "blocking_factor": "public_authority_applicant",
        }
    if not company_applicants:
        return {
            "outcome": "PERSON_OR_AGENT_ONLY" if clean_applicants or clean_agents else "NO_MATCH",
            "reason": (
                "only person/agent Planning evidence is available"
                if clean_applicants or clean_agents
                else "no Planning applicant organisation is stored locally"
            ),
            "applicant_evidence": clean_applicants,
            "agent_evidence": clean_agents,
            "proposed_organisation": None,
            "automation_allowed": False,
            "blocking_factor": "no_company_like_applicant",
        }

    matches: dict[str, dict[str, str]] = {}
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for applicant in company_applicants:
        candidates = organisations_by_identity.get(normalize_identity(applicant), [])
        unique = {candidate["id"]: candidate for candidate in candidates}
        if len(unique) == 1:
            candidate = next(iter(unique.values()))
            matches[candidate["id"]] = candidate
        elif len(unique) > 1:
            ambiguous.append(applicant)
        else:
            unmatched.append(applicant)
    if ambiguous or len(matches) > 1:
        return {
            "outcome": "AMBIGUOUS",
            "reason": "multiple existing organisations match the Planning applicant evidence",
            "applicant_evidence": clean_applicants,
            "agent_evidence": clean_agents,
            "proposed_organisation": None,
            "automation_allowed": False,
            "blocking_factor": "multiple_exact_organisation_matches",
        }
    if len(matches) == 1 and not unmatched:
        organisation = next(iter(matches.values()))
        return {
            "outcome": "EXACT / SAFE_AUTO_LINK",
            "reason": (
                "exact local organisation name or alias match for company-like Planning applicant"
            ),
            "applicant_evidence": clean_applicants,
            "agent_evidence": clean_agents,
            "proposed_organisation": organisation,
            "automation_allowed": True,
            "blocking_factor": None,
        }
    if len(matches) == 1:
        organisation = next(iter(matches.values()))
        return {
            "outcome": "STRONG / REVIEW_RECOMMENDED",
            "reason": "one exact local match but additional unmatched Planning applicant evidence",
            "applicant_evidence": clean_applicants,
            "agent_evidence": clean_agents,
            "proposed_organisation": organisation,
            "automation_allowed": False,
            "blocking_factor": "conflicting_applicant_evidence",
        }
    return {
        "outcome": "NO_MATCH",
        "reason": "company-like Planning applicant has no exact existing local organisation match",
        "applicant_evidence": clean_applicants,
        "agent_evidence": clean_agents,
        "proposed_organisation": None,
        "automation_allowed": False,
        "blocking_factor": "no_exact_local_organisation_match",
    }


def _organisation_identity_index(conn: Any) -> dict[str, list[dict[str, str]]]:
    operator_rows = conn.execute(
        """SELECT op.id, op.name, op.legal_name, oa.alias
           FROM operators op
           LEFT JOIN organisation_aliases oa ON oa.operator_id = op.id
           WHERE COALESCE(op.organisation_type, 'UNKNOWN') <> 'PUBLIC_AUTHORITY'"""
    ).fetchall()
    by_identity: dict[str, list[dict[str, str]]] = {}
    for operator_id, name, legal_name, alias in operator_rows:
        candidate = {"id": str(operator_id), "name": str(name)}
        for value in (name, legal_name, alias):
            identity = normalize_identity(value)
            if identity:
                by_identity.setdefault(identity, []).append(candidate)
    return by_identity


def _customer_operator_enrichment_rows(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT o.id, COALESCE(o.customer_title, o.name), o.postcode, o.town,
                  COALESCE(applicants.values, ARRAY[]::text[]),
                  COALESCE(agents.values, ARRAY[]::text[])
           FROM opportunities o
           LEFT JOIN LATERAL (
             SELECT array_agg(DISTINCT NULLIF(trim(
                        se.extracted_facts->'planning_party_provenance'->'applicant'->>'name'
                      ), '')) FILTER (WHERE NULLIF(trim(
                        se.extracted_facts->'planning_party_provenance'->'applicant'->>'name'
                      ), '') IS NOT NULL)
                      AS values
             FROM opportunity_signals os
             JOIN raw_signals rs ON rs.id = os.raw_signal_id
             LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
             WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
               AND rs.source_type = 'planning' AND se.review_status = 'APPROVED'
           ) applicants ON TRUE
           LEFT JOIN LATERAL (
             SELECT array_agg(DISTINCT NULLIF(trim(
                        se.extracted_facts->'planning_party_provenance'->'agent'->>'name'
                      ), '')) FILTER (WHERE NULLIF(trim(
                        se.extracted_facts->'planning_party_provenance'->'agent'->>'name'
                      ), '') IS NOT NULL)
                      AS values
             FROM opportunity_signals os
             JOIN raw_signals rs ON rs.id = os.raw_signal_id
             LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
             WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
               AND rs.source_type = 'planning' AND se.review_status = 'APPROVED'
           ) agents ON TRUE
           WHERE o.vertical = 'CHILDRENS_HOME'
             AND o.publication_status = 'PUBLISHED'
             AND o.merged_into_opportunity_id IS NULL
             AND o.operator_id IS NULL
             AND NULLIF(trim(o.operator_name), '') IS NULL
           ORDER BY o.latest_update_at DESC, o.id"""
    ).fetchall()
    by_identity = _organisation_identity_index(conn)
    items: list[dict[str, Any]] = []
    for opportunity_id, title, postcode, town, applicants, agents in rows:
        classification = classify_customer_operator_identity(
            applicants=list(applicants or []),
            agents=list(agents or []),
            organisations_by_identity=by_identity,
        )
        items.append(
            {
                "opportunity_id": str(opportunity_id),
                "customer_title": str(title),
                "postcode_district": postcode_district(postcode),
                "town": customer_safe_place(town),
                **classification,
            }
        )
    return items


def customer_operator_enrichment_preview(
    settings: Settings, *, sample_limit: int = 10
) -> dict[str, Any]:
    """Read-only local-only preview for safe customer operator identity links."""
    from collections import Counter

    bounded_sample_limit = min(max(int(sample_limit), 1), 25)
    with connection(settings) as conn:
        items = _customer_operator_enrichment_rows(conn)
    outcomes = Counter(item["outcome"] for item in items)
    safe = [item for item in items if item["automation_allowed"]]
    return {
        "schema_version": "signalhub-customer-operator-enrichment-preview-v1",
        "policy_version": CUSTOMER_OPERATOR_ENRICHMENT_POLICY_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "provider_calls": 0,
        "candidates": len(items),
        "outcome_counts": dict(sorted(outcomes.items())),
        "safe_auto_link_count": len(safe),
        "historical_manual_validation": {
            "status": "UNAVAILABLE",
            "reason": (
                "legacy opportunity-to-organisation links do not record applicant-role provenance"
            ),
        },
        "samples": {
            outcome: [item for item in items if item["outcome"] == outcome][:bounded_sample_limit]
            for outcome in sorted(outcomes)
        },
    }


def apply_customer_operator_enrichment(
    settings: Settings, *, actor: str, max_batch_size: int = 25
) -> dict[str, Any]:
    """Apply only the recomputed exact local links, in a bounded audited batch."""
    limit = min(max(int(max_batch_size), 1), 25)
    changed: list[dict[str, Any]] = []
    skipped = 0
    with connection(settings) as conn:
        candidates = [
            item for item in _customer_operator_enrichment_rows(conn) if item["automation_allowed"]
        ][:limit]
        for item in candidates:
            organisation = item["proposed_organisation"]
            row = conn.execute(
                """UPDATE opportunities SET operator_id = %s, operator_name = %s, updated_at = now()
                   WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                     AND publication_status = 'PUBLISHED'
                     AND operator_id IS NULL AND NULLIF(trim(operator_name), '') IS NULL
                   RETURNING id""",
                (organisation["id"], organisation["name"], item["opportunity_id"]),
            ).fetchone()
            if not row:
                skipped += 1
                continue
            audit = conn.execute(
                """INSERT INTO admin_audit_events
                   (action, actor, target_type, target_count, details, vertical)
                   VALUES ('customer_operator_identity_auto_linked', %s, 'opportunity', 1, %s,
                           'CHILDRENS_HOME') RETURNING id""",
                (
                    actor,
                    Jsonb(
                        {
                            "opportunity_id": item["opportunity_id"],
                            "operator_id": organisation["id"],
                            "operator_name": organisation["name"],
                            "policy_version": CUSTOMER_OPERATOR_ENRICHMENT_POLICY_VERSION,
                            "reason": item["reason"],
                            "applicant_evidence": item["applicant_evidence"],
                            "provider_calls": 0,
                        }
                    ),
                ),
            ).fetchone()
            changed.append(
                {
                    "opportunity_id": item["opportunity_id"],
                    "operator_id": organisation["id"],
                    "operator_name": organisation["name"],
                    "audit_event_id": str(audit[0]),
                }
            )
        conn.commit()
        remaining = len(_customer_operator_enrichment_rows(conn))
    return {
        "policy_version": CUSTOMER_OPERATOR_ENRICHMENT_POLICY_VERSION,
        "max_batch_size": limit,
        "examined": len(candidates),
        "linked": len(changed),
        "skipped": skipped,
        "remaining_unlinked": remaining,
        "provider_calls": 0,
        "items": changed,
    }


def _published_planning_party_rows(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT o.id, rs.id, rs.external_id, rs.metadata, se.extracted_facts
           FROM opportunities o
           JOIN opportunity_signals os ON os.opportunity_id = o.id AND os.status = 'ACTIVE'
           JOIN raw_signals rs ON rs.id = os.raw_signal_id AND rs.source_type = 'planning'
           JOIN signal_enrichments se ON se.raw_signal_id = rs.id AND se.review_status = 'APPROVED'
           WHERE o.vertical = 'CHILDRENS_HOME'
             AND o.publication_status = 'PUBLISHED'
             AND o.merged_into_opportunity_id IS NULL
           ORDER BY o.id, rs.id"""
    ).fetchall()
    items: list[dict[str, Any]] = []
    for opportunity_id, signal_id, external_id, metadata, facts in rows:
        facts = facts if isinstance(facts, dict) else {}
        existing = facts.get("planning_party_provenance")
        if isinstance(existing, dict) and existing.get("version"):
            provenance, category = existing, "ALREADY_POPULATED"
        else:
            provenance, category = extract_planning_party_provenance(
                metadata,
                raw_source_identifier=external_id or signal_id,
            )
        items.append(
            {
                "opportunity_id": str(opportunity_id),
                "signal_id": str(signal_id),
                "external_id": str(external_id or ""),
                "metadata": metadata,
                "provenance": provenance,
                "category": category,
                "already_populated": category == "ALREADY_POPULATED",
            }
        )
    return items


def _party_backfill_report(conn: Any, *, sample_limit: int) -> dict[str, Any]:
    from collections import Counter, defaultdict

    items = _published_planning_party_rows(conn)
    by_opportunity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_opportunity[item["opportunity_id"]].append(item)
    organisations = _organisation_identity_index(conn)
    expected_safe = 0
    for records in by_opportunity.values():
        applicants = [
            str((record["provenance"] or {}).get("applicant", {}).get("name") or "")
            for record in records
            if isinstance((record["provenance"] or {}).get("applicant"), dict)
        ]
        agents = [
            str((record["provenance"] or {}).get("agent", {}).get("name") or "")
            for record in records
            if isinstance((record["provenance"] or {}).get("agent"), dict)
        ]
        if classify_customer_operator_identity(
            applicants=applicants, agents=agents, organisations_by_identity=organisations
        )["automation_allowed"]:
            expected_safe += 1
    counts = Counter(item["category"] for item in items)
    company_like = sum(
        1
        for item in items
        if isinstance((item["provenance"] or {}).get("applicant"), dict)
        and bool(item["provenance"]["applicant"].get("company_like"))
    )
    person = sum(1 for item in items if item["category"] == "APPLICANT_PERSON_EXTRACTED")
    return {
        "policy_version": "planning-party-provenance-v1",
        "provider_calls": 0,
        "published_opportunities_examined": len(by_opportunity),
        "planning_signals_examined": len(items),
        "category_counts": dict(sorted(counts.items())),
        "company_like_applicants": company_like,
        "person_applicants": person,
        "expected_safe_auto_link_count": expected_safe,
        "samples": [
            {
                "opportunity_id": item["opportunity_id"],
                "signal_id": item["signal_id"],
                "external_id": item["external_id"],
                "category": item["category"],
                "provenance": item["provenance"],
            }
            for item in items[:sample_limit]
        ],
        "_items": items,
    }


def customer_planning_party_backfill_preview(
    settings: Settings, *, sample_limit: int = 10
) -> dict[str, Any]:
    """Preview a local-only structured party-provenance backfill."""
    with connection(settings) as conn:
        report = _party_backfill_report(conn, sample_limit=min(max(sample_limit, 1), 25))
    report.pop("_items", None)
    report.update(
        {
            "schema_version": "signalhub-planning-party-backfill-preview-v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
        }
    )
    return report


def apply_customer_planning_party_backfill(
    settings: Settings, *, actor: str, max_batch_size: int = 100
) -> dict[str, Any]:
    """Persist missing party provenance for a bounded set of published Planning signals."""
    limit = min(max(int(max_batch_size), 1), 100)
    with connection(settings) as conn:
        report = _party_backfill_report(conn, sample_limit=0)
        candidates = [item for item in report.pop("_items") if not item["already_populated"]][
            :limit
        ]
        changed: list[dict[str, str]] = []
        for item in candidates:
            if item["provenance"] is None:
                # Preserve malformed payloads untouched: they need a human/data repair path.
                continue
            row = conn.execute(
                """UPDATE signal_enrichments
                   SET extracted_facts = extracted_facts || %s, updated_at = now()
                   WHERE raw_signal_id = %s
                     AND review_status = 'APPROVED'
                     AND COALESCE(extracted_facts->'planning_party_provenance'->>'version', '') = ''
                   RETURNING raw_signal_id""",
                (Jsonb({"planning_party_provenance": item["provenance"]}), item["signal_id"]),
            ).fetchone()
            if row:
                changed.append({"signal_id": item["signal_id"], "category": item["category"]})
        audit = conn.execute(
            """INSERT INTO admin_audit_events
               (action, actor, target_type, target_count, details, vertical)
               VALUES ('planning_party_provenance_backfill', %s, 'raw_signal', %s, %s,
                       'CHILDRENS_HOME') RETURNING id""",
            (
                actor,
                len(changed),
                Jsonb(
                    {
                        "policy_version": "planning-party-provenance-v1",
                        "selected": len(candidates),
                        "persisted": len(changed),
                        "provider_calls": 0,
                        "category_counts": report["category_counts"],
                    }
                ),
            ),
        ).fetchone()
        conn.commit()
        remaining = sum(
            1 for item in _published_planning_party_rows(conn) if not item["already_populated"]
        )
    return {
        "policy_version": "planning-party-provenance-v1",
        "max_batch_size": limit,
        "selected": len(candidates),
        "persisted": len(changed),
        "skipped": len(candidates) - len(changed),
        "remaining_missing": remaining,
        "provider_calls": 0,
        "audit_event_id": str(audit[0]),
        "items": changed,
    }


def official_planning_party_preview(
    settings: Settings,
    *,
    actor: str,
    enabled: bool,
    limit: int = 20,
    max_per_authority: int = 2,
) -> dict[str, Any]:
    """Fetch a tightly bounded sample of exact official Idox detail URLs.

    This is deliberately preview-only: it creates no organisations, party facts,
    evidence documents or opportunity links.  One audit row records request
    accounting; source responses are not retained until a separately approved
    persistence/apply phase.
    """
    from collections import Counter, defaultdict

    if not enabled:
        return {
            "preview_only": True,
            "enabled": False,
            "provider_requests": 0,
            "reason": "explicit enabled=true is required; no scheduled execution exists",
        }
    # Public Authority portals do not offer a uniform response-time guarantee.
    # Ten retains an authority-diverse diagnostic sample while leaving robust headroom
    # under the API Lambda's 60-second ceiling.
    bounded_limit = min(max(int(limit), 1), 10)
    bounded_per_authority = min(max(int(max_per_authority), 1), 3)
    with connection(settings) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext('official_planning_party_preview'))"
        ).fetchone()[0]
        if not locked:
            raise ValueError("an official Planning-party preview is already starting")
        running = conn.execute(
            """SELECT id FROM admin_audit_events
               WHERE action = 'official_planning_party_preview'
                 AND details->>'state' = 'RUNNING'
                 AND created_at > now() - interval '15 minutes'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        if running:
            raise ValueError("an official Planning-party preview is already running")
        used_today = conn.execute(
            """SELECT COALESCE(sum(target_count), 0) FROM admin_audit_events
               WHERE action = 'official_planning_party_preview'
                 AND created_at >= date_trunc('day', now())"""
        ).fetchone()[0]
        if int(used_today or 0) + bounded_limit > 50:
            raise ValueError("official Planning-party daily preview request limit reached")
        rows = conn.execute(
            """SELECT o.id, rs.id, rs.external_id, rs.source_url,
                      COALESCE(rs.metadata->>'council', rs.metadata->>'local_authority', 'Unknown')
               FROM opportunities o
               JOIN opportunity_signals os ON os.opportunity_id = o.id AND os.status = 'ACTIVE'
               JOIN raw_signals rs ON rs.id = os.raw_signal_id AND rs.source_type = 'planning'
               JOIN signal_enrichments se
                 ON se.raw_signal_id = rs.id AND se.review_status = 'APPROVED'
               WHERE o.vertical = 'CHILDRENS_HOME' AND o.publication_status = 'PUBLISHED'
                 AND o.operator_id IS NULL AND NULLIF(trim(o.operator_name), '') IS NULL
                 AND COALESCE(
                       se.extracted_facts->'planning_party_provenance'->'applicant'->>'name', ''
                     ) = ''
                 AND rs.source_url ILIKE '%/applicationDetails.do?%'
               ORDER BY COALESCE(
                          rs.metadata->>'council', rs.metadata->>'local_authority', 'Unknown'
                        ),
                        rs.discovered_at DESC"""
        ).fetchall()
        selected: list[tuple[Any, ...]] = []
        per_authority: dict[str, int] = defaultdict(int)
        for row in rows:
            authority = str(row[4] or "Unknown")
            if per_authority[authority] >= bounded_per_authority:
                continue
            selected.append(row)
            per_authority[authority] += 1
            if len(selected) >= bounded_limit:
                break
        organisations_by_identity = _organisation_identity_index(conn)
        audit = conn.execute(
            """INSERT INTO admin_audit_events
               (action, actor, target_type, target_count, details, vertical)
               VALUES ('official_planning_party_preview', %s, 'raw_signal', 0, %s,
                       'CHILDRENS_HOME') RETURNING id""",
            (
                actor,
                Jsonb(
                    {
                        "source": "OFFICIAL_IDOX_PUBLIC_ACCESS",
                        "preview_only": True,
                        "state": "RUNNING",
                        "request_limit": bounded_limit,
                        "max_per_authority": bounded_per_authority,
                    }
                ),
            ),
        ).fetchone()
        conn.commit()

    # Four concurrent exact-page requests keep the deliberately small sample within
    # the backend Lambda's 60-second limit even when several authorities time out.
    # This is not a crawler: `selected` has already enforced the global and
    # per-authority request ceilings above.
    from concurrent.futures import ThreadPoolExecutor

    def fetch(row: tuple[Any, ...]) -> tuple[tuple[Any, ...], Any]:
        return row, fetch_idox_party_via_fetcher(settings, str(row[3]))

    fetched: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="official-party") as executor:
        for row, result in executor.map(fetch, selected):
            fetched[str(row[1])] = result

    results: list[dict[str, Any]] = []
    for opportunity_id, signal_id, external_id, source_url, authority in selected:
        result = fetched[str(signal_id)]
        provenance = official_party_provenance(result, application_reference=str(external_id or ""))
        identity = classify_customer_operator_identity(
            applicants=[result.applicant_name] if result.applicant_name else [],
            agents=[value for value in (result.agent_name, result.agent_company) if value],
            organisations_by_identity=organisations_by_identity,
        )
        results.append(
            {
                "opportunity_id": str(opportunity_id),
                "signal_id": str(signal_id),
                "planning_reference": str(external_id or ""),
                "authority": str(authority),
                "source_url": result.source_url,
                "fetch_status": result.outcome,
                "applicant_name": result.applicant_name,
                "applicant_company_like": bool(
                    (provenance.get("applicant") or {}).get("company_like")
                ),
                "agent_name": result.agent_name,
                "agent_company": result.agent_company,
                "extraction_confidence": (
                    "EXPLICIT_LABEL" if result.applicant_name or result.agent_name else "NONE"
                ),
                "operator_link_candidate": {
                    "outcome": identity["outcome"],
                    "automation_allowed": identity["automation_allowed"],
                    "proposed_organisation": identity["proposed_organisation"],
                    "reason": identity["reason"],
                },
                "detail": result.detail,
            }
        )
    counts = Counter(item["fetch_status"] for item in results)
    authority_outcomes = Counter(f"{item['authority']}:{item['fetch_status']}" for item in results)
    with connection(settings) as conn:
        conn.execute(
            """UPDATE admin_audit_events
               SET target_count = %s, details = %s
               WHERE id = %s""",
            (
                len(results),
                Jsonb(
                    {
                        "source": "OFFICIAL_IDOX_PUBLIC_ACCESS",
                        "preview_only": True,
                        "state": "COMPLETED",
                        "request_limit": bounded_limit,
                        "max_per_authority": bounded_per_authority,
                        "outcome_counts": dict(counts),
                        "authority_outcomes": dict(authority_outcomes),
                    }
                ),
                audit[0],
            ),
        )
        conn.commit()
    return {
        "schema_version": "signalhub-official-planning-party-preview-v1",
        "preview_only": True,
        "source_strategy": "OFFICIAL_IDOX_PUBLIC_ACCESS_EXACT_URL_ONLY",
        "provider_requests": len(results),
        "audit_event_id": str(audit[0]),
        "daily_request_limit": 50,
        "max_per_authority": bounded_per_authority,
        "candidate_authorities": len(per_authority),
        "outcome_counts": dict(sorted(counts.items())),
        "items": results,
    }


def official_planning_party_latest_report(settings: Settings) -> dict[str, Any]:
    """Return the last persisted preview accounting without fetching any pages."""
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT id, created_at, target_count, details
               FROM admin_audit_events
               WHERE action = 'official_planning_party_preview'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {
            "schema_version": "signalhub-official-planning-party-report-v1",
            "available": False,
            "provider_requests": 0,
        }
    audit_id, created_at, target_count, details = row
    details = details if isinstance(details, dict) else {}
    return {
        "schema_version": "signalhub-official-planning-party-report-v1",
        "available": details.get("state") == "COMPLETED",
        "audit_event_id": str(audit_id),
        "generated_at": created_at.isoformat(),
        "provider_requests": int(target_count or 0),
        "preview_only": details.get("preview_only") is True,
        "state": details.get("state"),
        "outcome_counts": details.get("outcome_counts", {}),
        "authority_outcomes": details.get("authority_outcomes", {}),
    }


def _published_quality_assessment(row: dict[str, Any], *, now: datetime) -> tuple[str, list[str]]:
    """Classify a published customer record without changing publication state."""
    reasons: list[str] = []
    location_present = any(
        (
            customer_safe_place(row.get("town")),
            customer_safe_place(row.get("local_authority"), authority=True),
            customer_safe_place(row.get("region")),
            postcode_district(row.get("postcode")),
        )
    )
    if not row.get("approved_signal_count"):
        reasons.append("no_current_approved_evidence")
    if str(row.get("customer_lifecycle_stage") or "") == "STOPPED":
        reasons.append("stopped_lifecycle")
    if str(row.get("customer_lifecycle_stage") or "") == "APPEAL_PENDING":
        reasons.append("lifecycle_requires_human_review")
    if str(row.get("customer_lifecycle_stage") or "") == "NEEDS_REVIEW" and any(
        str(row.get(field) or "").strip() for field in ("customer_title", "customer_summary")
    ):
        reasons.append("lifecycle_requires_human_review")
    if not location_present:
        reasons.append("missing_site_or_location_identity")
    if not str(row.get("operator_name") or "").strip():
        reasons.append("missing_organisation_identity")
    if not row.get("official_source_link_count"):
        reasons.append("missing_official_source_link")
    updated_at = row.get("latest_update_at")
    if updated_at and updated_at < now - timedelta(days=180):
        reasons.append("stale_over_180_days")
    if not (
        str(row.get("customer_title") or "").strip()
        or str(row.get("generated_title") or "").strip()
    ):
        reasons.append("missing_customer_title")
    if not (
        str(row.get("customer_summary") or "").strip()
        or str(row.get("generated_summary") or "").strip()
    ):
        reasons.append("missing_customer_summary")

    if {"no_current_approved_evidence", "stopped_lifecycle"} & set(reasons):
        return "SHOULD_NOT_CURRENTLY_BE_PUBLISHED", reasons
    if "lifecycle_requires_human_review" in reasons:
        return "MISLEADING_OR_STALE_CUSTOMER_WORDING", reasons
    if "missing_site_or_location_identity" in reasons:
        return "NEEDS_SITE_IDENTITY", reasons
    if "missing_organisation_identity" in reasons:
        return "NEEDS_ORGANISATION_IDENTITY", reasons
    if reasons:
        return "USABLE_WITH_MINOR_ENRICHMENT", reasons
    return "CUSTOMER_READY", reasons


def customer_publication_quality(settings: Settings, *, sample_limit: int = 5) -> dict[str, Any]:
    """Return a bounded, read-only quality audit for the customer-visible cohort."""
    bounded_sample_limit = min(max(int(sample_limit), 1), 10)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT o.id, o.customer_title, o.customer_summary, o.operator_name,
                      o.town, o.postcode, o.change_type,
                      COALESCE(o.customer_lifecycle_stage, o.lifecycle_stage),
                      o.latest_update_at, geo.region, geo.local_authority,
                      COALESCE(evidence.approved_signal_count, 0),
                      COALESCE(evidence.official_source_link_count, 0),
                      COALESCE(evidence.source_types, ARRAY[]::text[])
               FROM opportunities o
               LEFT JOIN LATERAL (
                 SELECT
                   (array_agg(NULLIF(rs.metadata->>'region', '')) FILTER
                     (WHERE rs.metadata->>'region' IS NOT NULL))[1] AS region,
                   (array_agg(NULLIF(COALESCE(rs.metadata->>'local_authority',
                     rs.metadata->>'council', rs.metadata->'authority'->>'name'), '')) FILTER
                     (WHERE COALESCE(rs.metadata->>'local_authority', rs.metadata->>'council',
                       rs.metadata->'authority'->>'name') IS NOT NULL))[1] AS local_authority
                 FROM opportunity_signals os
                 JOIN raw_signals rs ON rs.id = os.raw_signal_id
                 WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
               ) geo ON TRUE
               LEFT JOIN LATERAL (
                 SELECT count(*) FILTER (
                          WHERE se.review_status = 'APPROVED'
                            AND rs.source_type <> 'procurement'
                        ) AS approved_signal_count,
                        count(*) FILTER (
                          WHERE se.review_status = 'APPROVED'
                            AND rs.source_type <> 'procurement'
                            AND rs.source_url ~ '^https?://'
                        ) AS official_source_link_count,
                        array_agg(DISTINCT rs.source_type) FILTER (
                          WHERE se.review_status = 'APPROVED'
                            AND rs.source_type <> 'procurement'
                        ) AS source_types
                 FROM opportunity_signals os
                 JOIN raw_signals rs ON rs.id = os.raw_signal_id
                 LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                 WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
               ) evidence ON TRUE
               WHERE o.vertical = 'CHILDRENS_HOME'
                 AND o.publication_status = 'PUBLISHED'
                 AND o.merged_into_opportunity_id IS NULL
               ORDER BY o.latest_update_at DESC, o.id"""
        ).fetchall()
    fields = (
        "id",
        "customer_title",
        "customer_summary",
        "operator_name",
        "town",
        "postcode",
        "change_type",
        "customer_lifecycle_stage",
        "latest_update_at",
        "region",
        "local_authority",
        "approved_signal_count",
        "official_source_link_count",
        "source_types",
    )
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for value in rows:
        row = dict(zip(fields, value))
        projection = {
            **row,
            "source_types": list(row.get("source_types") or []),
        }
        row["generated_title"] = _customer_title(projection)
        row["generated_summary"] = generated_customer_summary(projection)
        quality, reasons = _published_quality_assessment(row, now=now)
        location = (
            customer_safe_place(row.get("town"))
            or customer_safe_place(row.get("local_authority"), authority=True)
            or customer_safe_place(row.get("region"))
        )
        items.append(
            {
                "opportunity_id": str(row["id"]),
                "quality": quality,
                "reasons": reasons,
                "lifecycle": row.get("customer_lifecycle_stage"),
                "source_types": projection["source_types"],
                "has_operator": bool(str(row.get("operator_name") or "").strip()),
                "has_location": bool(location or postcode_district(row.get("postcode"))),
                "latest_update_at": row.get("latest_update_at"),
                "customer_title": row["generated_title"],
            }
        )
    from collections import Counter

    quality_counts = Counter(item["quality"] for item in items)
    reason_counts = Counter(reason for item in items for reason in item["reasons"])
    lifecycle_counts = Counter(str(item["lifecycle"] or "UNSET") for item in items)
    source_counts = Counter(source for item in items for source in item["source_types"])
    duplicate_keys: dict[tuple[str, str], list[str]] = {}
    for item in items:
        key = (item["customer_title"].casefold(), str(item["lifecycle"] or ""))
        duplicate_keys.setdefault(key, []).append(item["opportunity_id"])
    duplicate_groups = [
        {"title": key[0], "lifecycle": key[1], "opportunity_ids": ids}
        for key, ids in duplicate_keys.items()
        if len(ids) > 1
    ]
    return {
        "schema_version": CUSTOMER_PUBLICATION_QUALITY_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "read_only": True,
        "customer_surface_verticals": ["CHILDRENS_HOME"],
        "published_total": len(items),
        "quality_counts": dict(sorted(quality_counts.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "source_type_counts": dict(sorted(source_counts.items())),
        "duplicate_looking_groups": len(duplicate_groups),
        "duplicate_looking_samples": duplicate_groups[:bounded_sample_limit],
        "samples": {
            quality: [item for item in items if item["quality"] == quality][:bounded_sample_limit]
            for quality in sorted(quality_counts)
        },
    }


def _neutral_needs_review_title(opportunity: dict[str, Any]) -> str:
    """Return a factual non-opening title for an unresolved published record."""
    location = customer_safe_location(
        town=opportunity.get("town"),
        local_authority=opportunity.get("local_authority"),
        region=opportunity.get("region"),
        postcode=opportunity.get("postcode"),
    )
    return f"Children’s home — {location}" if location else "Children’s home"


def _needs_review_wording_preview_item(opportunity: dict[str, Any]) -> dict[str, Any]:
    """Assess one published unresolved opportunity without changing business state."""
    relationships = opportunity.get("relationships") or []
    active = [item for item in relationships if item.get("status") == "ACTIVE"]
    support = [classify_evidence_support(item) for item in active]
    lifecycle = derive_care_lifecycle(active)
    provenance = opportunity.get("publication_automation_provenance") or {}
    automated = bool(provenance.get("policy_version"))
    protected = bool(opportunity.get("publication_automation_blocked")) or not automated
    stored_lifecycle = str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW")
    current_title = str(opportunity.get("customer_title") or "").strip() or None
    current_summary = str(opportunity.get("customer_summary") or "").strip() or None
    proposed_title = _neutral_needs_review_title(opportunity)
    proposed_summary = generated_customer_summary(
        {
            "customer_lifecycle_stage": "NEEDS_REVIEW",
            "foundational_evidence": EvidenceSupport.FOUNDATIONAL in support,
            "source_types": sorted(
                {str(item.get("source_type")) for item in active if item.get("source_type")}
            ),
        }
    )
    basis = current_opportunity_basis_reason(opportunity, active)

    if not current_title and not current_summary and lifecycle.lifecycle.value == "NEEDS_REVIEW":
        action = "SAFE_DERIVED_WORDING_FIX"
        reason = "current customer projection can safely use neutral unresolved-status wording"
        automation_allowed = True
    elif protected:
        action = "MANUAL_INVESTIGATION"
        reason = "manual/protected publication content must not be overwritten automatically"
        automation_allowed = False
    elif lifecycle.lifecycle.value != stored_lifecycle:
        action = "LIFECYCLE_RECALCULATION"
        reason = "stored lifecycle differs from the current deterministic lifecycle"
        automation_allowed = False
    elif opportunity.get("publication_status") != "PUBLISHED":
        action = "MANUAL_INVESTIGATION"
        reason = "opportunity is no longer currently published"
        automation_allowed = False
    else:
        action = "SAFE_DERIVED_WORDING_FIX"
        reason = "automatically published unresolved record requires neutral current-state wording"
        automation_allowed = True

    signals = [
        {
            "signal_id": str(item.get("id")),
            "title": item.get("title"),
            "review_status": item.get("review_status"),
            "source_type": item.get("source_type"),
            "planning_subtype": (item.get("extracted_facts") or {}).get("planning_subtype"),
            "planning_outcome": (item.get("metadata") or {}).get("planning_outcome"),
            "opportunity_action": (item.get("extracted_facts") or {}).get(
                "opportunity_creation_decision"
            ),
            "evidence_role": classify_evidence_support(item).value,
        }
        for item in active
    ]
    return {
        "opportunity_id": str(opportunity["id"]),
        "title": opportunity.get("name"),
        "publication_status": opportunity.get("publication_status"),
        "publication_provenance": "AUTOMATIC" if automated else "MANUAL_OR_PROTECTED",
        "customer_lifecycle": stored_lifecycle,
        "derived_lifecycle": lifecycle.lifecycle.value,
        "derived_lifecycle_reason": lifecycle.reason,
        "customer_title": current_title,
        "customer_summary": current_summary,
        "current_basis_reason": basis,
        "evidence_support": {
            "foundational": support.count(EvidenceSupport.FOUNDATIONAL),
            "supporting_followups": support.count(EvidenceSupport.SUPPORTING_FOLLOWUP),
            "other_lifecycle": support.count(EvidenceSupport.OTHER_LIFECYCLE),
        },
        "signals": signals,
        "quality_reason": "lifecycle_requires_human_review",
        "recommended_action": action,
        "automation_allowed": automation_allowed,
        "proposed_customer_title": proposed_title,
        "proposed_customer_summary": proposed_summary,
        "proposed_publication_impact": "NONE",
        "proposed_lifecycle_impact": (
            "NONE" if action != "LIFECYCLE_RECALCULATION" else "RECALCULATE"
        ),
        "reason": reason,
    }


def customer_needs_review_wording_preview(settings: Settings) -> dict[str, Any]:
    """Read-only projection audit for the bounded published NEEDS_REVIEW cohort."""
    from app.repository import _care_policy_opportunities

    with connection(settings) as conn:
        opportunities = _care_policy_opportunities(conn)
    items = [
        _needs_review_wording_preview_item(opportunity)
        for opportunity in opportunities
        if opportunity.get("publication_status") == "PUBLISHED"
        and str(opportunity.get("customer_lifecycle_stage") or "") == "NEEDS_REVIEW"
    ]
    return {
        "policy_version": CUSTOMER_NEEDS_REVIEW_WORDING_POLICY_VERSION,
        "preview_only": True,
        "published_needs_review_count": len(items),
        "items": items[:25],
    }


def pilot_curation_inventory(
    settings: Settings, *, limit: int = 100, publication_status: str | None = None
) -> dict[str, Any]:
    """Return a bounded internal-only inventory for the paid-pilot quality gate.

    This deliberately returns source summaries rather than raw evidence documents. It is invoked
    through the IAM-protected Lambda operational path, not exposed as a customer or admin HTTP API.
    """
    bounded_limit = min(max(int(limit), 1), 100)
    status = str(publication_status or "").strip().upper()
    if status and status not in {"DRAFT", "PUBLISHED", "WITHDRAWN"}:
        raise ValueError("invalid publication status")
    publication_clause = "AND o.publication_status = %s" if status else ""
    params: tuple[Any, ...] = (status, bounded_limit) if status else (bounded_limit,)
    with connection(settings) as conn:
        opportunity_rows = conn.execute(
            f"""SELECT o.id, o.name, o.customer_title, o.customer_summary,
                      o.operator_name, o.town, o.postcode, o.change_type,
                      o.lifecycle_stage, o.confidence, o.review_status,
                      o.publication_status, o.first_seen_at, o.latest_update_at,
                      o.location_sensitivity, o.creation_reason, o.stage_reason,
                      o.created_at
               FROM opportunities o
               WHERE o.vertical = 'CHILDRENS_HOME'
                 AND o.review_status NOT IN ('REJECTED', 'MERGED')
                 AND o.merged_into_opportunity_id IS NULL
                 {publication_clause}
               ORDER BY o.latest_update_at DESC, o.id
               LIMIT %s""",
            params,
        ).fetchall()
        evidence_rows = (
            conn.execute(
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
            ).fetchall()
            if opportunity_rows
            else []
        )

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
                "region": metadata.get("region"),
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
