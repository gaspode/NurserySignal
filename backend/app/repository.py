from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.classification import CLASSIFICATION_RULE_VERSION
from app.companies_house import OrganisationCandidate, compare_company_candidate
from app.config import Settings
from app.correlation import (
    classify_match,
    normalize_identity,
    preferred_match_reason,
    recruitment_evidence_strength,
)
from app.db import connection
from app.ingestion import NormalizedSignal
from app.queueing import EnrichmentMessage, send_enrichment_message
from app.recruitment import recruitment_record_from_signal
from app.verticals import (
    ALL_VERTICALS,
    NURSERY,
    policy_for,
    validate_vertical,
    validate_vertical_filter,
)


def record_admin_audit(
    settings: Settings,
    *,
    action: str,
    actor: str,
    target_type: str,
    details: dict[str, Any],
) -> str:
    """Write a small audit event for an administrative operation."""
    with connection(settings) as conn:
        row = conn.execute(
            """
            INSERT INTO admin_audit_events (action, actor, target_type, details)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (action, actor, target_type, Jsonb(details)),
        ).fetchone()
    return str(row[0])


def _postgres_text(value: Any) -> Any:
    """Remove source control bytes PostgreSQL cannot store from normalized fields."""
    if not isinstance(value, str):
        return value
    return value.replace("\x00", "")


def _validated_organisation_name(value: Any) -> str:
    """Reject parser run-on text before it becomes indexed organisation identity."""
    name = str(_postgres_text(value) or "").strip()
    if not name or len(name) > 300 or len(normalize_identity(name)) > 300:
        return ""
    return name


def _resolve_operator_id(conn: Any, name: Any, metadata: dict[str, Any]) -> Any | None:
    """Resolve a shared organisation without weakening vertical isolation."""
    display_name = str(name or "").strip()
    if not display_name:
        return None
    company_number = str(
        metadata.get("companies_house_number") or metadata.get("company_number") or ""
    ).strip() or None
    website = str(metadata.get("website") or metadata.get("website_url") or "").strip() or None
    identity = normalize_identity(display_name)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"operator:{identity}",))
    if company_number:
        row = conn.execute(
            "SELECT id FROM operators WHERE companies_house_number = %s", (company_number,)
        ).fetchone()
        if row:
            return row[0]
    rows = conn.execute(
        "SELECT id, name, legal_name FROM operators ORDER BY created_at LIMIT 1000"
    ).fetchall()
    for row in rows:
        if identity in {normalize_identity(row[1]), normalize_identity(row[2])}:
            return row[0]
    return conn.execute(
        """INSERT INTO operators (name, legal_name, companies_house_number, website_url)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (display_name, display_name, company_number, website),
    ).fetchone()[0]


def store_ofsted_urn_enrichment(
    settings: Settings,
    *,
    signal_id: str,
    value: dict[str, Any],
    evidence_bucket: str,
    evidence_key: str,
    content_sha256: str,
    retrieved_at: str,
) -> bool:
    """Persist immutable provider-level Ofsted evidence without changing site identity."""
    database_value = {
        key: _postgres_text(item) for key, item in value.items()
    }
    with connection(settings) as conn:
        signal = conn.execute(
            """SELECT vertical, source_type, organisation_hint
               FROM raw_signals WHERE id = %s FOR UPDATE""",
            (signal_id,),
        ).fetchone()
        if signal is None:
            raise ValueError("Ofsted enrichment references an unknown signal")
        if signal[0] != "CHILDRENS_HOME" or signal[1] != "ofsted":
            raise ValueError("Ofsted enrichment references an incompatible signal")
        observed_name = str(signal[2] or "").strip()
        provider_name = _validated_organisation_name(
            database_value.get("registered_provider_name")
        )
        operator_id = _resolve_operator_id(conn, observed_name or provider_name, {})
        if operator_id:
            for alias, source in (
                (observed_name, "OFSTED_REGISTER"),
                (provider_name, "OFSTED_REPORT"),
            ):
                safe_alias = _validated_organisation_name(alias)
                normalized = normalize_identity(safe_alias)
                if normalized:
                    conn.execute(
                        """INSERT INTO organisation_aliases
                           (operator_id, alias, normalized_alias, source)
                           VALUES (%s, %s, %s, %s)
                           ON CONFLICT (operator_id, normalized_alias) DO NOTHING""",
                        (operator_id, safe_alias, normalized, source),
                    )
        inserted = conn.execute(
            """INSERT INTO ofsted_urn_enrichments (
                   raw_signal_id, operator_id, urn, status, provider_page_url,
                   provision_type, registration_date, local_authority,
                   registered_provider_name, provider_registered_address,
                   provider_registered_locality, provider_registered_region,
                   provider_registered_postcode, latest_report_type,
                   latest_report_date, latest_report_publication_date,
                   latest_report_url, report_count, report_content_sha256,
                   parser_version, failure_category, evidence_bucket,
                   evidence_key, content_sha256, retrieved_at
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s,
                   %s, %s::date, %s::date, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s::timestamptz
               )
               ON CONFLICT (urn, content_sha256) DO NOTHING
               RETURNING id""",
            (
                signal_id,
                operator_id,
                database_value.get("urn"),
                database_value.get("status"),
                database_value.get("provider_page_url"),
                database_value.get("provision_type"),
                database_value.get("registration_date"),
                database_value.get("local_authority"),
                provider_name or None,
                database_value.get("provider_registered_address"),
                database_value.get("provider_registered_locality"),
                database_value.get("provider_registered_region"),
                database_value.get("provider_registered_postcode"),
                database_value.get("latest_report_type"),
                database_value.get("latest_report_date"),
                database_value.get("latest_report_publication_date"),
                database_value.get("latest_report_url"),
                int(database_value.get("report_count") or 0),
                database_value.get("report_content_sha256"),
                database_value.get("parser_version"),
                database_value.get("failure_category"),
                evidence_bucket,
                evidence_key,
                content_sha256,
                retrieved_at,
            ),
        ).fetchone()
        conn.commit()
    return inserted is not None


@dataclass(frozen=True)
class SignalIdentity:
    id: UUID
    evidence_key: str | None
    enrichment_queued_at: datetime | None
    content_sha256: str | None


@dataclass(frozen=True)
class StoredSignal:
    identity: SignalIdentity
    created: bool


@dataclass(frozen=True)
class ReprocessResult:
    operation_id: str
    selected: int
    pending_updated: int
    reviewed_preserved: int
    without_enrichment: int
    matched: int
    excluded: int


def find_signal(
    settings: Settings, vertical: str, source_type: str, external_id: str
) -> SignalIdentity | None:
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, sd.s3_key, rs.enrichment_queued_at, rs.content_sha256
            FROM raw_signals rs
            LEFT JOIN source_documents sd ON sd.raw_signal_id = rs.id
            WHERE rs.vertical = %s AND rs.source_type = %s AND rs.external_id = %s
            ORDER BY sd.captured_at DESC NULLS LAST
            LIMIT 1
            """,
            (vertical, source_type, external_id),
        ).fetchone()
    return _identity(row) if row else None


def store_signal(
    settings: Settings,
    signal: NormalizedSignal,
    bucket: str,
    key: str,
    content_sha256: str,
) -> StoredSignal:
    with connection(settings) as conn:
        row = conn.execute(
            """
            INSERT INTO raw_signals (
                schema_version, vertical, source_type, source_url, external_id, discovered_at,
                title, raw_text, location_hint, organisation_hint, metadata, content_sha256,
                location_sensitivity
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (vertical, source_type, external_id) DO NOTHING
            RETURNING id, enrichment_queued_at, content_sha256
            """,
            (
                signal.schema_version,
                signal.vertical,
                signal.source_type,
                signal.source_url,
                signal.external_id,
                signal.discovered_at,
                signal.title,
                signal.raw_text,
                signal.location_hint,
                signal.organisation_hint,
                Jsonb(signal.metadata),
                content_sha256,
                signal.metadata.get("location_sensitivity", "STANDARD"),
            ),
        ).fetchone()
        created = row is not None
        if row is None:
            row = conn.execute(
                """
                SELECT rs.id, rs.enrichment_queued_at, rs.content_sha256
                FROM raw_signals rs
                WHERE rs.vertical = %s AND rs.source_type = %s AND rs.external_id = %s
                """,
                (signal.vertical, signal.source_type, signal.external_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("signal disappeared during idempotent insert")
        signal_id, queued_at, stored_hash = row
        conn.execute(
            """
            INSERT INTO source_documents
                (raw_signal_id, vertical, s3_bucket, s3_key, sha256, mime_type)
            VALUES (%s, (SELECT vertical FROM raw_signals WHERE id = %s),
                    %s, %s, %s, 'application/json')
            ON CONFLICT (s3_bucket, s3_key) DO NOTHING
            """,
            (signal_id, signal_id, bucket, key, content_sha256),
        )
        conn.commit()
        evidence_row = conn.execute(
            """
            SELECT s3_key FROM source_documents
            WHERE raw_signal_id = %s ORDER BY created_at LIMIT 1
            """,
            (signal_id,),
        ).fetchone()
    return StoredSignal(
        identity=SignalIdentity(
            id=signal_id,
            evidence_key=evidence_row[0] if evidence_row else key,
            enrichment_queued_at=queued_at,
            content_sha256=stored_hash or content_sha256,
        ),
        created=created,
    )


def store_planning_revision(
    settings: Settings,
    signal: NormalizedSignal,
    signal_id: str,
    bucket: str,
    key: str,
    content_sha256: str,
) -> bool:
    """Track a changed planning record without creating another canonical signal."""
    metadata = signal.metadata
    with connection(settings) as conn:
        row = conn.execute(
            """
            INSERT INTO raw_signal_revisions (
                raw_signal_id, vertical, content_sha256, source_url, observed_at,
                planning_status, decision, metadata, evidence_bucket, evidence_key
            ) VALUES (%s, (SELECT vertical FROM raw_signals WHERE id = %s),
                      %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (raw_signal_id, content_sha256) DO NOTHING
            RETURNING id
            """,
            (
                signal_id,
                signal_id,
                content_sha256,
                signal.source_url,
                signal.discovered_at,
                metadata.get("planning_status"),
                metadata.get("decision"),
                Jsonb(metadata),
                bucket,
                key,
            ),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        conn.execute(
            """
            UPDATE raw_signals
            SET source_url = %s, discovered_at = %s, title = %s, raw_text = %s,
                location_hint = %s, organisation_hint = %s, metadata = %s,
                content_sha256 = %s
            WHERE id = %s
            """,
            (
                signal.source_url,
                signal.discovered_at,
                signal.title,
                signal.raw_text,
                signal.location_hint,
                signal.organisation_hint,
                Jsonb(signal.metadata),
                content_sha256,
                signal_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_documents
                (raw_signal_id, vertical, s3_bucket, s3_key, sha256, mime_type)
            VALUES (%s, (SELECT vertical FROM raw_signals WHERE id = %s),
                    %s, %s, %s, 'application/json')
            ON CONFLICT (s3_bucket, s3_key) DO NOTHING
            """,
            (signal_id, signal_id, bucket, key, content_sha256),
        )
        conn.commit()
    return True


def dispatch_enrichment(
    settings: Settings,
    identity: SignalIdentity,
    schema_version: str,
    bucket: str,
    key: str,
) -> bool:
    """Send once while holding a row lock, allowing retries after SQS failure."""
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT enrichment_queued_at FROM raw_signals WHERE id = %s FOR UPDATE",
            (identity.id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("cannot queue missing signal")
        if row[0] is not None:
            return False
        message = EnrichmentMessage(
            message_version="1.0",
            signal_id=str(identity.id),
            schema_version=schema_version,
            evidence_bucket=bucket,
            evidence_key=key,
            queued_at=datetime.now(UTC).isoformat(),
            vertical=str(
                conn.execute(
                    "SELECT vertical FROM raw_signals WHERE id = %s", (identity.id,)
                ).fetchone()[0]
            ),
        )
        send_enrichment_message(settings, message)
        conn.execute(
            "UPDATE raw_signals SET enrichment_queued_at = now() WHERE id = %s",
            (identity.id,),
        )
        conn.commit()
    return True


def dispatch_revision_enrichment(
    settings: Settings,
    identity: SignalIdentity,
    schema_version: str,
    bucket: str,
    key: str,
    vertical: str,
) -> None:
    """Queue one enrichment after a newly persisted mutable-source revision."""
    message = EnrichmentMessage(
        message_version="1.0",
        signal_id=str(identity.id),
        schema_version=schema_version,
        evidence_bucket=bucket,
        evidence_key=key,
        queued_at=datetime.now(UTC).isoformat(),
        vertical=validate_vertical(vertical),
    )
    send_enrichment_message(settings, message)


def get_raw_signal(settings: Settings, signal_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT id, schema_version, source_type, source_url, external_id, discovered_at,
                   title, raw_text, location_hint, organisation_hint, metadata, vertical,
                   location_sensitivity
            FROM raw_signals WHERE id = %s
            """,
            (signal_id,),
        ).fetchone()
    if row is None:
        return None
    keys = (
        "id",
        "schema_version",
        "source_type",
        "source_url",
        "external_id",
        "discovered_at",
        "title",
        "raw_text",
        "location_hint",
        "organisation_hint",
        "metadata",
        "vertical",
        "location_sensitivity",
    )
    return dict(zip(keys, row))


def list_vertical_backfill_candidates(
    settings: Settings,
    *,
    from_vertical: str,
    days: int,
    limit: int,
    recruitment_discovery_vertical: str | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded stored-evidence corpus without contacting a provider."""
    from_vertical = validate_vertical(from_vertical)
    days = min(max(days, 1), 90)
    limit = min(max(limit, 1), 50)
    discovery_clause = ""
    params: list[Any] = [from_vertical, days]
    if recruitment_discovery_vertical:
        discovery_vertical = validate_vertical(recruitment_discovery_vertical)
        discovery_clause = """
                 AND (
                    source_type <> 'recruitment'
                    OR COALESCE(metadata -> 'discovery_verticals', '[]'::jsonb) ? %s
                 )
        """
        params.append(discovery_vertical)
    params.append(limit)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""SELECT id, schema_version, source_type, source_url, external_id, discovered_at,
                      title, raw_text, location_hint, organisation_hint, metadata, vertical
               FROM raw_signals
               WHERE vertical = %s
                 AND source_type IN ('planning', 'recruitment')
                 AND discovered_at >= now() - (%s * interval '1 day')
                 {discovery_clause}
               ORDER BY discovered_at DESC, id DESC
               LIMIT %s""",
            params,
        ).fetchall()
    fields = (
        "id",
        "schema_version",
        "source_type",
        "source_url",
        "external_id",
        "discovered_at",
        "title",
        "raw_text",
        "location_hint",
        "organisation_hint",
        "metadata",
        "vertical",
    )
    return [dict(zip(fields, row)) for row in rows]


def list_signals(
    settings: Settings,
    *,
    limit: int,
    offset: int,
    review_status: str | None = None,
    source_type: str | None = None,
    discovered_from: str | None = None,
    discovered_to: str | None = None,
    search: str | None = None,
    unmatched_only: bool = False,
    include_excluded: bool = False,
    opportunity_decision: str | None = None,
    vertical: str | None = None,
) -> dict[str, Any]:
    vertical = validate_vertical_filter(vertical)
    clauses = ["TRUE"]
    params: list[Any] = []
    if vertical != ALL_VERTICALS:
        clauses.append("rs.vertical = %s")
        params.append(vertical)
    if review_status:
        if review_status == "REVIEWED":
            clauses.append("se.review_status IN ('APPROVED', 'REJECTED')")
        else:
            clauses.append("se.review_status = %s")
            params.append(review_status)
    if source_type:
        clauses.append("rs.source_type = %s")
        params.append(source_type)
    else:
        # Evaluation-only procurement evidence has its own admin surface and
        # must not inflate the human signal-review inbox or overview counts.
        clauses.append("rs.source_type <> 'procurement'")
    if discovered_from:
        clauses.append("rs.discovered_at >= %s::timestamptz")
        params.append(discovered_from)
    if discovered_to:
        clauses.append("rs.discovered_at < (%s::date + INTERVAL '1 day')")
        params.append(discovered_to)
    if search:
        search_pattern = f"%{search[:200]}%"
        clauses.append(
            "("
            "rs.title ILIKE %s OR rs.raw_text ILIKE %s OR rs.external_id ILIKE %s "
            "OR rs.location_hint ILIKE %s OR rs.organisation_hint ILIKE %s "
            "OR COALESCE(rs.metadata->>'council', '') ILIKE %s "
            "OR COALESCE(rs.metadata->>'postcode', '') ILIKE %s "
            "OR COALESCE(se.nursery_name, '') ILIKE %s "
            "OR COALESCE(se.operator_name, '') ILIKE %s "
            "OR COALESCE(se.address, '') ILIKE %s"
            ")"
        )
        params.extend([search_pattern] * 10)
    if unmatched_only:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM opportunity_signals active_os "
            "WHERE active_os.raw_signal_id = rs.id AND active_os.status = 'ACTIVE')"
        )
        if not include_excluded:
            clauses.append("COALESCE(se.review_status, '') <> 'REJECTED'")
            clauses.append(
                "COALESCE(se.extracted_facts->>'likely_false_positive', 'false') <> 'true'"
            )
            clauses.append(
                "COALESCE(se.extracted_facts->>'opportunity_creation_decision', '') "
                "<> 'IGNORE_FOR_OPPORTUNITY'"
            )
    if opportunity_decision:
        clauses.append("se.extracted_facts->>'opportunity_creation_decision' = %s")
        params.append(opportunity_decision)
    where = " AND ".join(clauses)
    with connection(settings) as conn:
        total = conn.execute(
            f"""
            SELECT count(*) FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE {where}
            """,
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url, rs.external_id,
                   rs.discovered_at, rs.title, rs.location_hint, rs.organisation_hint,
                   rs.metadata, rs.vertical, rs.location_sensitivity, rs.created_at,
                   rs.enrichment_queued_at,
                   se.review_status,
                   se.event_type, se.nursery_name, se.operator_name, se.lifecycle_stage,
                   se.confidence, se.extracted_facts, ai.recommendation,
                   ai.confidence, ai.status
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT recommendation, confidence, status
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC
                LIMIT 1
            ) ai ON TRUE
            WHERE {where}
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
    fields = (
        "id",
        "schema_version",
        "source_type",
        "source_url",
        "external_id",
        "discovered_at",
        "title",
        "location_hint",
        "organisation_hint",
        "metadata",
        "vertical",
        "location_sensitivity",
        "created_at",
        "enrichment_queued_at",
        "review_status",
        "event_type",
        "nursery_name",
        "operator_name",
        "lifecycle_stage",
        "confidence",
        "extracted_facts",
        "ai_recommendation",
        "ai_confidence",
        "ai_status",
    )
    return {
        "items": [dict(zip(fields, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_procurement_evaluations(
    settings: Settings, *, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Return bounded CareSignal shadow records with conservative incremental context."""
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    with connection(settings) as conn:
        total = conn.execute(
            """SELECT count(*) FROM raw_signals
               WHERE vertical = 'CHILDRENS_HOME' AND source_type = 'procurement'"""
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT rs.id, rs.title, rs.source_url, rs.discovered_at,
                      rs.organisation_hint, rs.location_hint, rs.metadata,
                      se.confidence, se.extracted_facts,
                      (SELECT count(*) FROM raw_signals related
                       WHERE related.vertical = rs.vertical
                         AND related.source_type IN ('planning', 'recruitment')
                         AND lower(COALESCE(related.organisation_hint, '')) =
                             lower(COALESCE(rs.organisation_hint, ''))
                         AND related.id <> rs.id) AS related_signal_count,
                      (SELECT count(DISTINCT os.opportunity_id)
                       FROM raw_signals related
                       JOIN opportunity_signals os ON os.raw_signal_id = related.id
                                                   AND os.status = 'ACTIVE'
                       WHERE related.vertical = rs.vertical
                         AND related.source_type IN ('planning', 'recruitment')
                         AND lower(COALESCE(related.organisation_hint, '')) =
                             lower(COALESCE(rs.organisation_hint, ''))) AS related_opportunity_count
               FROM raw_signals rs
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'procurement'
               ORDER BY rs.discovered_at DESC, rs.id DESC
               LIMIT %s OFFSET %s""",
            (limit, offset),
        ).fetchall()
    fields = (
        "id",
        "title",
        "source_url",
        "publication_date",
        "buyer",
        "location",
        "metadata",
        "confidence",
        "extracted_facts",
        "related_signal_count",
        "related_opportunity_count",
    )
    items = [dict(zip(fields, row)) for row in rows]
    counts: dict[str, int] = {}
    for item in items:
        category = str((item.get("metadata") or {}).get("procurement_category") or "UNCERTAIN")
        counts[category] = counts.get(category, 0) + 1
        item["appears_incremental"] = not (
            item["related_signal_count"] or item["related_opportunity_count"]
        )
        item["operator_known"] = bool((item.get("metadata") or {}).get("suppliers"))
    return {"items": items, "total": total, "limit": limit, "offset": offset, "counts": counts}


def signal_detail(settings: Settings, signal_id: str) -> dict[str, Any] | None:
    raw = get_raw_signal(settings, signal_id)
    if raw is None:
        return None
    with connection(settings) as conn:
        documents = conn.execute(
            """
            SELECT id, s3_bucket, s3_key, sha256, mime_type, captured_at
            FROM source_documents WHERE raw_signal_id = %s ORDER BY captured_at
            """,
            (signal_id,),
        ).fetchall()
        enrichment = conn.execute(
            """
            SELECT id, schema_version, event_type, nursery_name, operator_name, address,
                   expected_opening_date, capacity, lifecycle_stage, confidence,
                   extracted_facts, evidence, review_status, reviewed_by, reviewed_at,
                   created_at, updated_at
            FROM signal_enrichments WHERE raw_signal_id = %s
            """,
            (signal_id,),
        ).fetchone()
        revisions = conn.execute(
            """
            SELECT id, content_sha256, source_url, observed_at, planning_status,
                   decision, evidence_bucket, evidence_key, created_at
            FROM raw_signal_revisions
            WHERE raw_signal_id = %s
            ORDER BY observed_at DESC
            """,
            (signal_id,),
        ).fetchall()
        ai_reviews = conn.execute(
            """SELECT id, provider, model_id, prompt_version, recommendation, confidence,
                      reason, recruitment_relevance, planning_relevance, commercial_change_evidence,
                      status, failure_category,
                      attempted_at, evaluated_at,
                      input_tokens, output_tokens, latency_ms, created_at
               FROM signal_ai_reviews WHERE raw_signal_id = %s ORDER BY created_at DESC""",
            (signal_id,),
        ).fetchall()
        ofsted_enrichment = conn.execute(
            """SELECT urn, status, provider_page_url, provision_type,
                      registration_date, local_authority, registered_provider_name,
                      provider_registered_address, provider_registered_locality,
                      provider_registered_region, provider_registered_postcode,
                      latest_report_type, latest_report_date,
                      latest_report_publication_date, latest_report_url,
                      report_count, parser_version, failure_category, retrieved_at
               FROM ofsted_urn_enrichments
               WHERE raw_signal_id = %s
               ORDER BY retrieved_at DESC, created_at DESC LIMIT 1""",
            (signal_id,),
        ).fetchone()
    raw["documents"] = [
        dict(zip(("id", "s3_bucket", "s3_key", "sha256", "mime_type", "captured_at"), row))
        for row in documents
    ]
    raw["planning_revisions"] = [
        dict(
            zip(
                (
                    "id",
                    "content_sha256",
                    "source_url",
                    "observed_at",
                    "planning_status",
                    "decision",
                    "evidence_bucket",
                    "evidence_key",
                    "created_at",
                ),
                row,
            )
        )
        for row in revisions
    ]
    raw["ai_reviews"] = [
        dict(
            zip(
                (
                    "id",
                    "provider",
                    "model_id",
                    "prompt_version",
                    "recommendation",
                    "confidence",
                    "reason",
                    "recruitment_relevance",
                    "planning_relevance",
                    "commercial_change_evidence",
                    "status",
                    "failure_category",
                    "attempted_at",
                    "evaluated_at",
                    "input_tokens",
                    "output_tokens",
                    "latency_ms",
                    "created_at",
                ),
                row,
            )
        )
        for row in ai_reviews
    ]
    if ofsted_enrichment:
        raw["ofsted_enrichment"] = dict(
            zip(
                (
                    "urn",
                    "status",
                    "provider_page_url",
                    "provision_type",
                    "registration_date",
                    "local_authority",
                    "registered_provider_name",
                    "provider_registered_address",
                    "provider_registered_locality",
                    "provider_registered_region",
                    "provider_registered_postcode",
                    "latest_report_type",
                    "latest_report_date",
                    "latest_report_publication_date",
                    "latest_report_url",
                    "report_count",
                    "parser_version",
                    "failure_category",
                    "retrieved_at",
                ),
                ofsted_enrichment,
            )
        )
    if enrichment:
        raw["enrichment"] = dict(
            zip(
                (
                    "id",
                    "schema_version",
                    "event_type",
                    "nursery_name",
                    "operator_name",
                    "address",
                    "expected_opening_date",
                    "capacity",
                    "lifecycle_stage",
                    "confidence",
                    "extracted_facts",
                    "evidence",
                    "review_status",
                    "reviewed_by",
                    "reviewed_at",
                    "created_at",
                    "updated_at",
                ),
                enrichment,
            )
        )
    else:
        raw["enrichment"] = None
    return raw


def reprocess_planning_signals(
    settings: Settings,
    *,
    actor: str,
    limit: int,
    discovered_from: str | None = None,
    discovered_to: str | None = None,
    signal_ids: list[str] | None = None,
) -> ReprocessResult:
    """Re-evaluate stored planning evidence without re-ingesting it.

    This function never writes S3 or sends SQS messages.  A single audit row
    records the bounded operation, while pending candidates are updated in
    place and reviewed decisions remain unchanged.
    """
    clauses = ["rs.source_type = 'planning'"]
    params: list[Any] = []
    if discovered_from:
        clauses.append("rs.discovered_at >= %s::timestamptz")
        params.append(discovered_from)
    if discovered_to:
        clauses.append("rs.discovered_at < (%s::date + INTERVAL '1 day')")
        params.append(discovered_to)
    if signal_ids:
        clauses.append("rs.id = ANY(%s::uuid[])")
        params.append(signal_ids)
    where = " AND ".join(clauses)

    with connection(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url,
                   rs.external_id, rs.discovered_at, rs.title, rs.raw_text,
                   rs.location_hint, rs.organisation_hint, rs.metadata, rs.vertical,
                   se.review_status
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE {where}
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            [*params, limit],
        ).fetchall()
        operation_id = conn.execute(
            """
            INSERT INTO admin_audit_events (action, actor, target_type, details)
            VALUES ('planning_reprocess', %s, 'raw_signal', '{}'::jsonb)
            RETURNING id
            """,
            (actor,),
        ).fetchone()[0]

        pending_updated = 0
        reviewed_preserved = 0
        without_enrichment = 0
        matched = 0
        excluded = 0
        for row in rows:
            raw = dict(
                zip(
                    (
                        "id",
                        "schema_version",
                        "source_type",
                        "source_url",
                        "external_id",
                        "discovered_at",
                        "title",
                        "raw_text",
                        "location_hint",
                        "organisation_hint",
                        "metadata",
                        "vertical",
                        "review_status",
                    )
                    if len(row) == 13
                    else (
                        "id",
                        "schema_version",
                        "source_type",
                        "source_url",
                        "external_id",
                        "discovered_at",
                        "title",
                        "raw_text",
                        "location_hint",
                        "organisation_hint",
                        "metadata",
                        "review_status",
                    ),
                    row,
                )
            )
            if raw["review_status"] is None:
                without_enrichment += 1
                continue
            candidate = policy_for(str(raw.get("vertical") or NURSERY)).classify_signal(raw)
            candidate_matched = candidate["extracted_facts"].get("planning_candidate_matched")
            if candidate_matched:
                matched += 1
            else:
                excluded += 1
            if raw["review_status"] == "PENDING":
                conn.execute(
                    """
                    UPDATE signal_enrichments
                    SET schema_version = %s, event_type = %s, nursery_name = %s,
                        operator_name = %s, address = %s, expected_opening_date = %s,
                        capacity = %s, lifecycle_stage = %s, confidence = %s,
                        extracted_facts = %s, evidence = %s, updated_at = now()
                    WHERE raw_signal_id = %s AND review_status = 'PENDING'
                    """,
                    (
                        candidate["schema_version"],
                        candidate["event_type"],
                        candidate["nursery_name"],
                        candidate["operator_name"],
                        candidate["address"],
                        candidate["expected_opening_date"],
                        candidate["capacity"],
                        candidate["lifecycle_stage"],
                        candidate["confidence"],
                        Jsonb(candidate["extracted_facts"]),
                        Jsonb(candidate["evidence"]),
                        raw["id"],
                    ),
                )
                pending_updated += 1
            else:
                # A reviewed candidate is historical.  Retain its decision and
                # displayed fields, but retain a compact latest derived check
                # for auditability and later operator inspection.
                evaluation = {
                    "classification_rule_version": CLASSIFICATION_RULE_VERSION,
                    "planning_candidate_matched": candidate_matched,
                    "likely_false_positive": candidate["extracted_facts"].get(
                        "likely_false_positive", False
                    ),
                    "classification": candidate["extracted_facts"].get("classification"),
                    "evaluated_at": datetime.now(UTC).isoformat(),
                    "operation_id": str(operation_id),
                }
                conn.execute(
                    """
                    UPDATE signal_enrichments
                    SET extracted_facts = extracted_facts || %s, updated_at = now()
                    WHERE raw_signal_id = %s AND review_status IN ('APPROVED', 'REJECTED')
                    """,
                    (Jsonb({"latest_reprocess_evaluation": evaluation}), raw["id"]),
                )
                reviewed_preserved += 1

        details = {
            "limit": limit,
            "discovered_from": discovered_from,
            "discovered_to": discovered_to,
            "explicit_id_count": len(signal_ids or []),
            "selected": len(rows),
            "pending_updated": pending_updated,
            "reviewed_preserved": reviewed_preserved,
            "without_enrichment": without_enrichment,
            "matched": matched,
            "excluded": excluded,
            "classification_rule_version": CLASSIFICATION_RULE_VERSION,
        }
        conn.execute(
            """
            UPDATE admin_audit_events
            SET target_count = %s, details = %s
            WHERE id = %s
            """,
            (len(rows), Jsonb(details), operation_id),
        )
        conn.commit()
    return ReprocessResult(
        operation_id=str(operation_id),
        selected=len(rows),
        pending_updated=pending_updated,
        reviewed_preserved=reviewed_preserved,
        without_enrichment=without_enrichment,
        matched=matched,
        excluded=excluded,
    )


def reprocess_recruitment_signals(
    settings: Settings,
    *,
    actor: str,
    limit: int,
    signal_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Reclassify stored recruitment evidence without re-ingesting it."""
    clauses = ["rs.source_type = 'recruitment'"]
    params: list[Any] = []
    if signal_ids:
        clauses.append("rs.id = ANY(%s::uuid[])")
        params.append(signal_ids)
    where = " AND ".join(clauses)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url,
                   rs.external_id, rs.discovered_at, rs.title, rs.raw_text,
                   rs.location_hint, rs.organisation_hint, rs.metadata, rs.vertical,
                   se.review_status
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE {where}
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            [*params, limit],
        ).fetchall()
        operation_id = conn.execute(
            """
            INSERT INTO admin_audit_events (action, actor, target_type, details)
            VALUES ('recruitment_reprocess', %s, 'raw_signal', '{}'::jsonb)
            RETURNING id
            """,
            (actor,),
        ).fetchone()[0]
        pending_updated = 0
        reviewed_preserved = 0
        without_enrichment = 0
        matched = 0
        excluded = 0
        for row in rows:
            raw = dict(
                zip(
                    (
                        "id",
                        "schema_version",
                        "source_type",
                        "source_url",
                        "external_id",
                        "discovered_at",
                        "title",
                        "raw_text",
                        "location_hint",
                        "organisation_hint",
                        "metadata",
                        "vertical",
                        "review_status",
                    )
                    if len(row) == 13
                    else (
                        "id",
                        "schema_version",
                        "source_type",
                        "source_url",
                        "external_id",
                        "discovered_at",
                        "title",
                        "raw_text",
                        "location_hint",
                        "organisation_hint",
                        "metadata",
                        "review_status",
                    ),
                    row,
                )
            )
            if raw["review_status"] is None:
                without_enrichment += 1
                continue
            recruitment_record = recruitment_record_from_signal(raw)
            normalized_metadata = dict(raw["metadata"] or {})
            normalized_metadata.update(
                {
                    "postcode": recruitment_record.postcode,
                    "latitude": recruitment_record.latitude,
                    "longitude": recruitment_record.longitude,
                    "locality": recruitment_record.locality,
                    "region": recruitment_record.region,
                }
            )
            normalized_location = (
                ", ".join(
                    value
                    for value in (
                        recruitment_record.address,
                        recruitment_record.postcode,
                        recruitment_record.locality,
                        recruitment_record.region,
                    )
                    if value
                )
                or raw["location_hint"]
            )
            conn.execute(
                """
                UPDATE raw_signals
                SET location_hint = %s, metadata = %s
                WHERE id = %s AND source_type = 'recruitment'
                """,
                (normalized_location, Jsonb(normalized_metadata), raw["id"]),
            )
            raw["location_hint"] = normalized_location
            raw["metadata"] = normalized_metadata
            candidate = policy_for(str(raw.get("vertical") or NURSERY)).classify_signal(raw)
            candidate_matched = bool(
                candidate["extracted_facts"].get("recruitment_candidate_matched")
            )
            matched += int(candidate_matched)
            excluded += int(not candidate_matched)
            if raw["review_status"] == "PENDING":
                conn.execute(
                    """
                    UPDATE signal_enrichments
                    SET schema_version = %s, event_type = %s, nursery_name = %s,
                        operator_name = %s, address = %s, expected_opening_date = %s,
                        capacity = %s, lifecycle_stage = %s, confidence = %s,
                        extracted_facts = %s, evidence = %s, updated_at = now()
                    WHERE raw_signal_id = %s AND review_status = 'PENDING'
                    """,
                    (
                        candidate["schema_version"],
                        candidate["event_type"],
                        candidate["nursery_name"],
                        candidate["operator_name"],
                        candidate["address"],
                        candidate["expected_opening_date"],
                        candidate["capacity"],
                        candidate["lifecycle_stage"],
                        candidate["confidence"],
                        Jsonb(candidate["extracted_facts"]),
                        Jsonb(candidate["evidence"]),
                        raw["id"],
                    ),
                )
                pending_updated += 1
            else:
                evaluation = {
                    "recruitment_rule_version": "recruitment-v2",
                    "recruitment_candidate_matched": candidate_matched,
                    "relevance": candidate["extracted_facts"].get("recruitment_relevance"),
                    "commercial_change_evidence": candidate["extracted_facts"].get(
                        "commercial_change_evidence"
                    ),
                    "evaluated_at": datetime.now(UTC).isoformat(),
                    "operation_id": str(operation_id),
                }
                conn.execute(
                    """
                    UPDATE signal_enrichments
                    SET extracted_facts = extracted_facts || %s, updated_at = now()
                    WHERE raw_signal_id = %s AND review_status IN ('APPROVED', 'REJECTED')
                    """,
                    (Jsonb({"latest_recruitment_reprocess_evaluation": evaluation}), raw["id"]),
                )
                reviewed_preserved += 1
        details = {
            "limit": limit,
            "explicit_id_count": len(signal_ids or []),
            "selected": len(rows),
            "pending_updated": pending_updated,
            "reviewed_preserved": reviewed_preserved,
            "without_enrichment": without_enrichment,
            "matched": matched,
            "excluded": excluded,
            "recruitment_rule_version": "recruitment-v2",
        }
        conn.execute(
            "UPDATE admin_audit_events SET target_count = %s, details = %s WHERE id = %s",
            (len(rows), Jsonb(details), operation_id),
        )
        conn.commit()
    return {"operation_id": str(operation_id), **details}


def review_signal(settings: Settings, signal_id: str, status: str, reviewer: str | None) -> bool:
    with connection(settings) as conn:
        row = conn.execute(
            """
            UPDATE signal_enrichments
            SET review_status = %s, reviewed_by = %s, reviewed_at = now(), updated_at = now()
            WHERE raw_signal_id = %s
            RETURNING id
            """,
            (status, reviewer, signal_id),
        ).fetchone()
        conn.commit()
    return row is not None


def review_signals_bulk(
    settings: Settings,
    signal_ids: list[str],
    status: str,
    reviewer: str | None,
) -> dict[str, Any]:
    """Apply one review decision to a bounded pending set and audit the action."""
    with connection(settings) as conn:
        rows = conn.execute(
            """
            UPDATE signal_enrichments
            SET review_status = %s, reviewed_by = %s, reviewed_at = now(), updated_at = now()
            WHERE raw_signal_id = ANY(%s::uuid[]) AND review_status = 'PENDING'
            RETURNING raw_signal_id
            """,
            (status, reviewer, signal_ids),
        ).fetchall()
        updated_ids = [str(row[0]) for row in rows]
        conn.execute(
            """
            INSERT INTO admin_audit_events (action, actor, target_type, target_count, details)
            VALUES ('BULK_REVIEW', %s, 'signal', %s, %s)
            """,
            (
                reviewer or "unknown",
                len(updated_ids),
                Jsonb(
                    {
                        "status": status,
                        "requested_count": len(signal_ids),
                        "updated_count": len(updated_ids),
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "status": status,
        "requested": len(signal_ids),
        "updated": len(updated_ids),
        "skipped": len(signal_ids) - len(updated_ids),
        "signal_ids": updated_ids,
    }


def save_enrichment(settings: Settings, candidate: dict[str, Any]) -> bool:
    with connection(settings) as conn:
        row = conn.execute(
            """
            INSERT INTO signal_enrichments (
                raw_signal_id, vertical, schema_version, event_type, nursery_name,
                operator_name, address,
                expected_opening_date, capacity, lifecycle_stage, confidence,
                extracted_facts, evidence, review_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
            ON CONFLICT (raw_signal_id) DO NOTHING
            RETURNING id
            """,
            (
                candidate["raw_signal_id"],
                (candidate.get("metadata") or {}).get("vertical", NURSERY),
                candidate["schema_version"],
                candidate["event_type"],
                candidate["nursery_name"],
                candidate["operator_name"],
                candidate["address"],
                candidate["expected_opening_date"],
                candidate["capacity"],
                candidate["lifecycle_stage"],
                candidate["confidence"],
                Jsonb(candidate["extracted_facts"]),
                Jsonb(candidate["evidence"]),
            ),
        ).fetchone()
        if row is None and str((candidate.get("metadata") or {}).get("source_type")) == "ofsted":
            row = conn.execute(
                """UPDATE signal_enrichments SET schema_version = %s, event_type = %s,
                   nursery_name = %s, operator_name = %s, address = %s,
                   expected_opening_date = %s, capacity = %s, lifecycle_stage = %s,
                   confidence = %s, extracted_facts = %s, evidence = %s, updated_at = now()
                   WHERE raw_signal_id = %s RETURNING id""",
                (
                    candidate["schema_version"],
                    candidate["event_type"],
                    candidate["nursery_name"],
                    candidate["operator_name"],
                    candidate["address"],
                    candidate["expected_opening_date"],
                    candidate["capacity"],
                    candidate["lifecycle_stage"],
                    candidate["confidence"],
                    Jsonb(candidate["extracted_facts"]),
                    Jsonb(candidate["evidence"]),
                    candidate["raw_signal_id"],
                ),
            ).fetchone()
        conn.commit()
    return row is not None


def correlate_signal(
    settings: Settings, signal_id: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    """Create/link an opportunity only when the source supports that decision."""
    metadata = candidate.get("metadata") or {}
    postcode = metadata.get("postcode")
    name = (
        candidate.get("nursery_name")
        or candidate.get("operator_name")
        or candidate.get("address")
        or "Unmatched signal"
    )
    operator = candidate.get("operator_name")
    base_confidence = float(candidate.get("confidence") or 0.2)
    source_type = str(metadata.get("source_type") or candidate.get("source_type") or "")
    recruitment_contribution, recruitment_reason = recruitment_evidence_strength(candidate)
    if source_type == "recruitment" and recruitment_contribution == 0:
        base_confidence = min(base_confidence, 0.35)
    elif source_type == "recruitment" and recruitment_contribution == 0.05:
        base_confidence = min(base_confidence, 0.55)
    with connection(settings) as conn:
        # Serialise matching for one signal. This protects the check-then-create
        # path when a worker retry and an administrator reprocess overlap.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(signal_id),))
        signal_vertical = conn.execute(
            "SELECT vertical FROM raw_signals WHERE id = %s", (signal_id,)
        ).fetchone()
        if signal_vertical is None:
            raise ValueError("signal_not_found")
        signal_vertical = validate_vertical(str(signal_vertical[0] or NURSERY))
        candidate_vertical = validate_vertical(
            str((candidate.get("metadata") or {}).get("vertical") or signal_vertical)
        )
        if candidate_vertical != signal_vertical:
            raise ValueError("signal and candidate verticals must match")
        creation = policy_for(signal_vertical).determine_opportunity_action(candidate)
        active = conn.execute(
            """SELECT os.opportunity_id FROM opportunity_signals os
               JOIN opportunities o ON o.id = os.opportunity_id
               WHERE os.raw_signal_id = %s AND os.status = 'ACTIVE'
               ORDER BY os.created_at, os.opportunity_id
               LIMIT 1""",
            (signal_id,),
        ).fetchone()
        if active:
            canonical = _canonical_opportunity_id(conn, active[0])
            conn.commit()
            return {
                "opportunity_id": str(canonical or active[0]),
                "linked": True,
                "reused": True,
                "relationship_created": False,
                "created": False,
                "creation_decision": creation.decision,
                "reason": "signal already has an active opportunity relationship",
            }
        if creation.decision in {"IGNORE_FOR_OPPORTUNITY", "REVIEW"}:
            conn.commit()
            return {
                "opportunity_id": None,
                "linked": False,
                "reused": False,
                "relationship_created": False,
                "created": False,
                "creation_decision": creation.decision,
                "reason": creation.reason,
            }
        existing = conn.execute(
            """SELECT o.id, o.name, o.operator_id, o.lifecycle_stage, o.confidence,
                      o.confidence_breakdown, COALESCE(n.postcode, linked.postcode),
                      COALESCE(op.legal_name, op.name, linked.operator_name), o.town
               FROM opportunities o
               LEFT JOIN nurseries n ON n.id = o.nursery_id
               LEFT JOIN operators op ON op.id = o.operator_id
               LEFT JOIN LATERAL (
                 SELECT rs.metadata->>'postcode' AS postcode,
                        COALESCE(se.operator_name, rs.organisation_hint) AS operator_name
                 FROM opportunity_signals os JOIN raw_signals rs ON rs.id = os.raw_signal_id
                 LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                 WHERE os.opportunity_id = o.id ORDER BY rs.discovered_at LIMIT 1
               ) linked ON TRUE
                 WHERE o.review_status NOT IN ('MERGED', 'REJECTED')
                   AND o.vertical = %s
                 ORDER BY o.updated_at DESC"""
            , (signal_vertical,)).fetchall()
        match = None
        match_reason = None
        uncertain_matches: list[tuple[Any, float, str]] = []
        if source_type == "ofsted":
            area = normalize_identity(metadata.get("local_authority"))
            organisation = normalize_identity(operator)
            strong = [
                row
                for row in existing
                if organisation
                and organisation == normalize_identity(row[7])
                and area
                and area == normalize_identity(row[8])
            ]
            possible = [
                row
                for row in existing
                if organisation and organisation == normalize_identity(row[7])
            ]
            for row in (strong or possible)[:3]:
                uncertain_matches.append(
                    (
                        row[0],
                        0.78 if row in strong else 0.62,
                        "registered provider matches but Ofsted's published location "
                        "is insufficient to identify one residential site safely",
                    )
                )
        else:
            for row in existing:
                comparison = classify_match(postcode, name, row[6], row[7] or row[1])
                operator_comparison = classify_match(postcode, operator, row[6], row[7])
                if (
                    comparison.outcome == "UNCERTAIN"
                    or operator_comparison.outcome == "UNCERTAIN"
                ):
                    uncertain = (
                        comparison
                        if comparison.outcome == "UNCERTAIN"
                        else operator_comparison
                    )
                    uncertain_matches.append((row[0], uncertain.confidence, uncertain.reason))
                if comparison.outcome == "EXACT" or operator_comparison.outcome == "EXACT":
                    blocked = conn.execute(
                        """SELECT 1 FROM opportunity_signals
                           WHERE opportunity_id = %s AND raw_signal_id = %s
                             AND status = 'REJECTED'""",
                        (row[0], signal_id),
                    ).fetchone()
                    if blocked:
                        continue
                    match = row
                    match_reason = preferred_match_reason(comparison, operator_comparison)
                    break
        if match:
            opportunity_id = match[0]
            breakdown = dict(match[5] or {})
            evidence = breakdown.setdefault("evidence", [])
            if signal_id not in {
                item.get("signal_id") for item in evidence if isinstance(item, dict)
            }:
                evidence.append(
                    {
                        "signal_id": signal_id,
                        "source_type": source_type,
                        "confidence": base_confidence,
                        "reason": match_reason,
                    }
                )
            unique_sources = {
                item.get("source_type") for item in evidence if isinstance(item, dict)
            }
            confidence = min(0.98, max(float(match[4] or 0), base_confidence))
            if source_type == "recruitment":
                confidence = min(0.98, confidence + recruitment_contribution)
            stage = (
                "REGISTRATION"
                if source_type == "ofsted"
                else "STAFFING"
                if len(unique_sources) > 1
                and source_type == "recruitment"
                and recruitment_contribution > 0.05
                else match[3]
            )
            conn.execute(
                """UPDATE opportunities SET confidence = %s, lifecycle_stage = %s,
                   confidence_breakdown = %s, stage_reason = %s,
                   latest_update_at = now(), updated_at = now()
                   WHERE id = %s""",
                (
                    confidence,
                    stage,
                    Jsonb(breakdown),
                    f"{match_reason}; {recruitment_reason}"
                    if source_type == "recruitment"
                    else "Ofsted evidence confirms registration progress"
                    if source_type == "ofsted"
                    else match_reason,
                    opportunity_id,
                ),
            )
        elif creation.decision == "CREATE_OPPORTUNITY":
            vertical_policy = policy_for(signal_vertical)
            display_name = vertical_policy.build_opportunity_title(candidate, creation)
            operator_id = _resolve_operator_id(conn, operator, metadata)
            opportunity_id = conn.execute(
                """INSERT INTO opportunities
                   (name, operator_id, event_type, lifecycle_stage, confidence,
                    confidence_breakdown, stage_reason, creation_reason, vertical,
                    operator_name, address, postcode, town, change_type, location_sensitivity)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    display_name,
                    operator_id,
                    candidate.get("event_type") or "other",
                    vertical_policy.initial_lifecycle(source_type),
                    base_confidence,
                    Jsonb(
                        {
                            "evidence": [
                                {
                                    "signal_id": signal_id,
                                    "source_type": source_type,
                                    "confidence": base_confidence,
                                    "reason": "initial signal",
                                }
                            ],
                            "independence": 1,
                        }
                    ),
                    "planning evidence" if source_type == "planning" else recruitment_reason,
                    creation.reason,
                    signal_vertical,
                    operator,
                    candidate.get("address"),
                    postcode,
                    metadata.get("town") or metadata.get("locality"),
                    creation.change_type,
                    "INTERNAL_EXACT" if signal_vertical == "CHILDRENS_HOME" else "STANDARD",
                ),
            ).fetchone()[0]
        else:
            for possible_id, possible_confidence, possible_reason in uncertain_matches[:3]:
                if _canonical_opportunity_id(conn, possible_id) != possible_id:
                    continue
                conn.execute(
                    """INSERT INTO opportunity_match_reviews
                       (raw_signal_id, opportunity_id, vertical, outcome, confidence, reason)
                       VALUES (%s, %s, %s, 'UNCERTAIN', %s, %s)
                       ON CONFLICT (raw_signal_id, opportunity_id) DO NOTHING""",
                    (
                        signal_id,
                        possible_id,
                        signal_vertical,
                        possible_confidence,
                        possible_reason,
                    ),
                )
            conn.commit()
            return {
                "opportunity_id": None,
                "linked": False,
                "reused": False,
                "relationship_created": False,
                "created": False,
                "creation_decision": creation.decision,
                "reason": creation.reason,
            }
        conn.execute(
            """INSERT INTO opportunity_signals (
                   opportunity_id, raw_signal_id, vertical, relationship_type,
                   extracted_facts, provenance, status, created_by,
                   match_outcome, match_confidence, match_reason)
               VALUES (%s, %s, %s, 'SUPPORTS', %s, %s, 'ACTIVE', 'SYSTEM', %s, %s, %s)
               ON CONFLICT (opportunity_id, raw_signal_id) DO NOTHING""",
            (
                opportunity_id,
                signal_id,
                signal_vertical,
                Jsonb(candidate.get("extracted_facts") or {}),
                Jsonb({"method": "deterministic-v1", "reason": match_reason or "initial signal"}),
                "STRONG" if match else "NO_MATCH",
                base_confidence,
                match_reason or "initial signal",
            ),
        )
        for possible_id, possible_confidence, possible_reason in uncertain_matches[:3]:
            if _canonical_opportunity_id(conn, possible_id) != possible_id:
                continue
            if conn.execute(
                """SELECT 1 FROM opportunity_signals
                   WHERE raw_signal_id = %s AND opportunity_id = %s AND status = 'ACTIVE'""",
                (signal_id, possible_id),
            ).fetchone():
                continue
            conn.execute(
                """INSERT INTO opportunity_match_reviews
                   (raw_signal_id, opportunity_id, vertical, outcome, confidence, reason)
                   VALUES (%s, %s, %s, 'UNCERTAIN', %s, %s)
                   ON CONFLICT (raw_signal_id, opportunity_id) DO NOTHING""",
                (signal_id, possible_id, signal_vertical, possible_confidence, possible_reason),
            )
        conn.commit()
    return {
        "opportunity_id": str(opportunity_id),
        "linked": bool(match),
        "reused": bool(match),
        "relationship_created": True,
        "created": not bool(match),
        "creation_decision": creation.decision,
        "reason": match_reason or "initial signal",
    }


def _canonical_opportunity_id(conn: Any, opportunity_id: Any) -> Any | None:
    """Resolve a merged opportunity to its current canonical record.

    A bounded loop protects matching from malformed consolidation chains. A
    cycle is treated as unresolved rather than granting a stale record current
    status.
    """
    current = opportunity_id
    seen: set[str] = set()
    for _ in range(20):
        if current is None or str(current) in seen:
            return None
        seen.add(str(current))
        row = conn.execute(
            """SELECT id, review_status, merged_into_opportunity_id
               FROM opportunities WHERE id = %s""",
            (current,),
        ).fetchone()
        if not row:
            return None
        if row[1] != "MERGED" or row[2] is None:
            return row[0]
        current = row[2]
    return None


def _safe_system_duplicate_key(row: tuple[Any, ...]) -> tuple[str, str, str, str, str] | None:
    """Build a conservative identity from opportunity and linked-signal data.

    Row positions are: id, vertical, postcode, effective operator,
    change_type, created_at, effective site. Operator identity wins; when it
    is absent, a strong site identity may be used only alongside postcode.
    """
    vertical, postcode, operator, change_type, site = row[1], row[2], row[3], row[4], row[6]
    normalized_postcode = normalize_identity(postcode)
    normalized_operator = normalize_identity(operator)
    normalized_site = normalize_identity(site)
    generic_sites = {
        "new nursery",
        "nursery setting",
        "nursery commercial change",
        "nursery capacity expansion",
    }
    if not normalized_postcode:
        return None
    if normalized_operator:
        identity_type, identity = "operator", normalized_operator
    elif normalized_site and normalized_site not in generic_sites:
        identity_type, identity = "site", normalized_site
    else:
        return None
    return (
        str(vertical or "NURSERY"),
        normalized_postcode,
        identity_type,
        identity,
        str(change_type or "OTHER_CHANGE"),
    )


def _duplicate_group_keys(row: tuple[Any, ...]) -> list[tuple[str, str, str, str, str]]:
    """Return stable identity keys, including an exact shared-signal key.

    A shared active signal is the strongest possible duplicate indicator: one
    source record must not be actively attached to two system opportunities.
    """
    keys: list[tuple[str, str, str, str, str]] = []
    identity_key = _safe_system_duplicate_key(row)
    if identity_key:
        keys.append(identity_key)
    vertical = str(row[1] or "NURSERY")
    change_type = str(row[4] or "OTHER_CHANGE")
    for signal_id in row[7] or []:
        keys.append((vertical, "shared_signal", str(signal_id), change_type, ""))
    return keys


def _consolidate_system_duplicates(settings: Settings, *, actor: str) -> int:
    """Merge only exact, system-created duplicate opportunity groups.

    Display names alone are deliberately not used as identity. Groups require
    the same vertical, postcode, stable operator identity (or a strong stored
    site identity when operator data is absent) and change type. Every active
    relationship must remain system-owned with no admin history.
    """
    merged = 0
    with connection(settings) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("opportunity-dedupe",))
        rows = conn.execute(
            """SELECT o.id, o.vertical, o.postcode,
                      COALESCE(o.operator_name, linked.operator_name),
                      o.change_type, o.created_at,
                      COALESCE(o.address, linked.site_identity, o.name),
                      (SELECT array_agg(os2.raw_signal_id::text ORDER BY os2.raw_signal_id)
                       FROM opportunity_signals os2
                       WHERE os2.opportunity_id = o.id AND os2.status = 'ACTIVE')
               FROM opportunities o
               LEFT JOIN LATERAL (
                   SELECT COALESCE(se.operator_name, rs.organisation_hint) AS operator_name,
                          COALESCE(rs.location_hint, rs.metadata->>'address') AS site_identity
                   FROM opportunity_signals os
                   JOIN raw_signals rs ON rs.id = os.raw_signal_id
                   LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                   ORDER BY rs.discovered_at, rs.id
                   LIMIT 1
               ) linked ON TRUE
               WHERE o.review_status NOT IN ('MERGED', 'REJECTED')
               ORDER BY o.created_at, o.id"""
        ).fetchall()
        groups: dict[tuple[str, str, str, str, str], list[tuple[Any, ...]]] = {}
        for row in rows:
            for key in _duplicate_group_keys(row):
                groups.setdefault(key, []).append(row)
        merged_opportunity_ids: set[Any] = set()
        for group in groups.values():
            group = [row for row in group if row[0] not in merged_opportunity_ids]
            if len(group) < 2:
                continue
            target = group[0]
            opportunity_ids = [row[0] for row in group]
            active_links = conn.execute(
                """SELECT opportunity_id, created_by FROM opportunity_signals
                   WHERE opportunity_id = ANY(%s::uuid[]) AND status = 'ACTIVE'""",
                (opportunity_ids,),
            ).fetchall()
            if any(created_by != "SYSTEM" for _, created_by in active_links):
                continue
            if conn.execute(
                """SELECT 1 FROM opportunity_signal_history
                   WHERE opportunity_id = ANY(%s::uuid[]) AND action LIKE 'ADMIN%%'
                   LIMIT 1""",
                (opportunity_ids,),
            ).fetchone():
                continue
            if conn.execute(
                """SELECT 1 FROM opportunity_match_reviews
                   WHERE opportunity_id = ANY(%s::uuid[]) AND status = 'REJECTED'
                   LIMIT 1""",
                (opportunity_ids,),
            ).fetchone():
                continue
            for source_id, *_ in group[1:]:
                source_links = conn.execute(
                    """SELECT raw_signal_id, relationship_type, extracted_facts,
                              provenance, match_outcome, match_confidence, match_reason
                       FROM opportunity_signals
                       WHERE opportunity_id = %s AND status = 'ACTIVE'""",
                    (source_id,),
                ).fetchall()
                for (
                    signal_id,
                    relationship_type,
                    extracted_facts,
                    provenance,
                    match_outcome,
                    match_confidence,
                    match_reason,
                ) in source_links:
                    _archive_relationship(
                        conn,
                        source_id,
                        signal_id,
                        "SYSTEM_DEDUPLICATE",
                        actor,
                        f"consolidated into {target[0]}",
                    )
                    target_link = conn.execute(
                        """SELECT 1 FROM opportunity_signals
                           WHERE opportunity_id = %s AND raw_signal_id = %s
                             AND status = 'ACTIVE'""",
                        (target[0], signal_id),
                    ).fetchone()
                    if target_link:
                        conn.execute(
                            """UPDATE opportunity_signals SET status = 'REJECTED',
                               match_reason = %s
                               WHERE opportunity_id = %s AND raw_signal_id = %s""",
                            (
                                f"duplicate relationship consolidated into {target[0]}",
                                source_id,
                                signal_id,
                            ),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO opportunity_signals
                               (opportunity_id, raw_signal_id, relationship_type,
                                extracted_facts, provenance, status, created_by,
                                match_outcome, match_confidence, match_reason)
                               VALUES (%s, %s, %s, %s, %s, 'ACTIVE', 'SYSTEM', %s, %s, %s)""",
                            (
                                target[0],
                                signal_id,
                                relationship_type,
                                Jsonb(extracted_facts or {}),
                                Jsonb(
                                    {
                                        **(provenance or {}),
                                        "deduplicated_from": str(source_id),
                                    }
                                ),
                                match_outcome,
                                match_confidence,
                                f"consolidated from duplicate opportunity {source_id}",
                            ),
                        )
                    conn.execute(
                        """UPDATE opportunity_signals SET status = 'REJECTED',
                           match_reason = %s WHERE opportunity_id = %s AND raw_signal_id = %s""",
                        (f"consolidated into {target[0]}", source_id, signal_id),
                    )
                conn.execute(
                    """UPDATE opportunities SET review_status = 'MERGED',
                       merged_into_opportunity_id = %s, updated_at = now()
                       WHERE id = %s""",
                    (target[0], source_id),
                )
                merged_opportunity_ids.add(source_id)
                merged += 1
            conn.execute(
                """UPDATE opportunities target
                   SET first_seen_at = bounds.first_seen_at,
                       latest_update_at = bounds.latest_update_at,
                       confidence = GREATEST(target.confidence, bounds.max_confidence),
                       updated_at = now()
                   FROM (
                       SELECT min(first_seen_at) AS first_seen_at,
                              max(latest_update_at) AS latest_update_at,
                              max(confidence) AS max_confidence
                       FROM opportunities
                       WHERE id = ANY(%s::uuid[])
                   ) bounds
                   WHERE target.id = %s""",
                (opportunity_ids, target[0]),
            )
        conn.commit()
    return merged


def recalculate_opportunity_creation(
    settings: Settings,
    *,
    actor: str,
    limit: int = 25,
    offset: int = 0,
    signal_ids: list[str] | None = None,
    vertical: str = "ALL",
) -> dict[str, Any]:
    """Boundedly recalculate opportunity creation from stored signal evidence."""
    limit = min(max(limit, 1), 25)
    offset = min(max(offset, 0), 75)
    vertical = validate_vertical_filter(vertical)
    merged = _consolidate_system_duplicates(settings, actor=actor) if offset == 0 else 0
    review_cleanup = (
        _cleanup_match_reviews(settings, limit=limit, actor=actor)
        if offset == 0
        else {"closed_superseded": 0, "closed_linked": 0}
    )
    with connection(settings) as conn:
        params: list[Any] = []
        where = "rs.source_type IN ('planning', 'recruitment')"
        if vertical != "ALL":
            where += " AND rs.vertical = %s"
            params.append(vertical)
        if signal_ids:
            where += " AND rs.id = ANY(%s::uuid[])"
            params.append(signal_ids[:limit])
        rows = conn.execute(
            f"""SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url, rs.external_id,
                       rs.discovered_at, rs.title, rs.raw_text, rs.location_hint,
                       rs.organisation_hint, rs.metadata, rs.vertical, se.review_status
                FROM raw_signals rs LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                WHERE {where} ORDER BY rs.discovered_at DESC, rs.id LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        ).fetchall()
    selected = 0
    created = 0
    linked = 0
    reused = 0
    relationships_created = 0
    routine_only_demoted = 0
    review_required = 0
    unchanged = 0
    for row in rows:
        selected += 1
        raw = dict(
            zip(
                (
                    "id",
                    "schema_version",
                    "source_type",
                    "source_url",
                    "external_id",
                    "discovered_at",
                    "title",
                    "raw_text",
                    "location_hint",
                    "organisation_hint",
                    "metadata",
                    "vertical",
                    "review_status",
                ),
                row,
            )
        )
        vertical_policy = policy_for(str(raw.get("vertical") or NURSERY))
        candidate = vertical_policy.classify_signal(raw)
        candidate["metadata"] = {
            **(raw.get("metadata") or {}),
            "source_type": raw.get("source_type"),
            "vertical": raw.get("vertical", NURSERY),
        }
        facts = candidate["extracted_facts"]
        creation = vertical_policy.determine_opportunity_action(candidate)
        with connection(settings) as conn:
            conn.execute(
                """UPDATE signal_enrichments
                   SET extracted_facts = COALESCE(extracted_facts, '{}'::jsonb) || %s
                   WHERE raw_signal_id = %s""",
                (Jsonb(facts), raw["id"]),
            )
            demoted = conn.execute(
                """SELECT o.id
                   FROM opportunities o JOIN opportunity_signals os ON os.opportunity_id = o.id
                   JOIN raw_signals old_rs ON old_rs.id = os.raw_signal_id
                   JOIN signal_enrichments old_se ON old_se.raw_signal_id = old_rs.id
                   WHERE os.raw_signal_id = %s AND os.status = 'ACTIVE' AND os.created_by = 'SYSTEM'
                     AND old_rs.source_type = 'recruitment'
                     AND COALESCE(old_se.extracted_facts->>'recruitment_relevance', '')
                         = 'RELEVANT_ROUTINE'
                     AND COALESCE(old_se.extracted_facts->>'commercial_change_evidence', 'NONE')
                         = 'NONE'
                     AND (SELECT count(*) FROM opportunity_signals active_os
                          WHERE active_os.opportunity_id = o.id AND active_os.status = 'ACTIVE') = 1
                     AND NOT EXISTS (SELECT 1 FROM opportunity_signal_history h
                                     WHERE h.opportunity_id = o.id AND h.action LIKE 'ADMIN%%')""",
                (raw["id"],),
            ).fetchall()
            for (opportunity_id,) in demoted:
                _archive_relationship(
                    conn,
                    opportunity_id,
                    raw["id"],
                    "SYSTEM_RECALCULATE",
                    actor,
                    "routine recruitment does not create an opportunity",
                )
                conn.execute(
                    """UPDATE opportunity_signals SET status = 'REJECTED', match_reason = %s
                       WHERE opportunity_id = %s AND raw_signal_id = %s""",
                    (
                        "routine recruitment does not create an opportunity",
                        opportunity_id,
                        raw["id"],
                    ),
                )
                conn.execute(
                    """UPDATE opportunities
                       SET review_status = 'REJECTED', updated_at = now()
                       WHERE id = %s""",
                    (opportunity_id,),
                )
                routine_only_demoted += 1
            if creation.decision == "CREATE_OPPORTUNITY":
                conn.execute(
                    """UPDATE opportunities
                       SET name = %s, change_type = %s, updated_at = now()
                       WHERE id IN (
                           SELECT os.opportunity_id FROM opportunity_signals os
                           WHERE os.raw_signal_id = %s AND os.status = 'ACTIVE'
                             AND os.created_by = 'SYSTEM'
                       )
                         AND NOT EXISTS (
                             SELECT 1 FROM opportunity_signal_history h
                             WHERE h.opportunity_id = opportunities.id AND h.action LIKE 'ADMIN%%'
                         )""",
                    (
                        vertical_policy.build_opportunity_title(candidate, creation),
                        creation.change_type,
                        raw["id"],
                    ),
                )
            conn.commit()
        result = correlate_signal(settings, str(raw["id"]), candidate)
        if result.get("created"):
            created += 1
        elif result.get("linked"):
            linked += 1
            if result.get("reused"):
                reused += 1
        if result.get("relationship_created"):
            relationships_created += 1
        elif result.get("creation_decision") == "REVIEW":
            review_required += 1
        else:
            unchanged += 1
    operation = record_admin_audit(
        settings,
        action="opportunity_creation_recalculate",
        actor=actor,
        target_type="signal",
        details={
            "selected": selected,
            "offset": offset,
            "vertical": vertical,
            "created": created,
            "linked": linked,
            "reused": reused,
            "relationships_created": relationships_created,
            "merged": merged,
            "routine_only_demoted": routine_only_demoted,
            "review_required": review_required,
            "unchanged": unchanged,
            "match_reviews_closed_superseded": review_cleanup["closed_superseded"],
            "match_reviews_closed_linked": review_cleanup["closed_linked"],
            "bounded": True,
        },
    )
    return {
        "operation_id": operation,
        "selected": selected,
        "offset": offset,
        "vertical": vertical,
        "created": created,
        "linked": linked,
        "reused": reused,
        "relationships_created": relationships_created,
        "merged": merged,
        "routine_only_demoted": routine_only_demoted,
        "review_required": review_required,
        "unchanged": unchanged,
        "match_reviews_closed_superseded": review_cleanup["closed_superseded"],
        "match_reviews_closed_linked": review_cleanup["closed_linked"],
    }


def list_opportunities(
    settings: Settings, *, limit: int, offset: int, search: str | None = None,
    vertical: str | None = None,
) -> dict[str, Any]:
    vertical = validate_vertical_filter(vertical)
    clauses = ["TRUE"]
    params: list[Any] = []
    if vertical != ALL_VERTICALS:
        clauses.append("o.vertical = %s")
        params.append(vertical)
    if search:
        clauses.append(
            "(o.name ILIKE %s OR COALESCE(o.creation_reason, '') ILIKE %s "
            "OR COALESCE(o.stage_reason, '') ILIKE %s)"
        )
        pattern = f"%{search[:200]}%"
        params.extend([pattern, pattern, pattern])
    where = " AND ".join(clauses)
    with connection(settings) as conn:
        total = conn.execute(
            f"""SELECT count(*) FROM opportunities o
                WHERE o.review_status NOT IN ('MERGED', 'REJECTED') AND {where}""",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT o.id, o.name, o.operator_id, o.operator_name, o.address, o.postcode, o.town,
                       o.vertical, o.change_type, o.event_type, o.lifecycle_stage, o.confidence,
                       o.confidence_breakdown, o.stage_reason, o.creation_reason,
                       o.first_seen_at, o.latest_update_at, o.location_sensitivity,
                       count(os.raw_signal_id) FILTER (WHERE os.status = 'ACTIVE')
                FROM opportunities o LEFT JOIN opportunity_signals os ON os.opportunity_id = o.id
                WHERE o.review_status NOT IN ('MERGED', 'REJECTED') AND {where}
                GROUP BY o.id ORDER BY o.latest_update_at DESC LIMIT %s OFFSET %s""",
            [*params, limit, offset],
        ).fetchall()
    fields = (
        "id",
        "name",
        "operator_id",
        "operator_name",
        "address",
        "postcode",
        "town",
        "vertical",
        "change_type",
        "event_type",
        "lifecycle_stage",
        "confidence",
        "confidence_breakdown",
        "stage_reason",
        "creation_reason",
        "first_seen_at",
        "latest_update_at",
        "location_sensitivity",
        "signal_count",
    )
    return {
        "items": [dict(zip(fields, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def list_organisations(
    settings: Settings, *, limit: int, offset: int, vertical: str | None = None
) -> dict[str, Any]:
    """Show shared organisation identities with vertical-scoped activity."""
    vertical = validate_vertical_filter(vertical)
    clauses = ["TRUE"]
    params: list[Any] = []
    if vertical != ALL_VERTICALS:
        clauses.append("activity.vertical = %s")
        params.append(vertical)
    where = " AND ".join(clauses)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""WITH activity AS (
                    SELECT vertical, organisation_hint AS name, id AS signal_id,
                           NULL::uuid AS opportunity_id
                    FROM raw_signals WHERE organisation_hint IS NOT NULL
                    UNION ALL
                    SELECT vertical, operator_name AS name, NULL::uuid, id
                    FROM opportunities WHERE operator_name IS NOT NULL
                ), grouped AS (
                  SELECT min(name) AS name, vertical,
                         count(DISTINCT signal_id) AS signal_count,
                         count(DISTINCT opportunity_id) AS opportunity_count
                  FROM activity WHERE {where}
                  GROUP BY vertical, lower(trim(name))
                )
                SELECT g.name, g.vertical, g.signal_count, g.opportunity_count,
                       op.id, op.legal_name, op.companies_house_number,
                       op.company_status, op.incorporation_date,
                       op.companies_house_url, op.resolution_outcome
                FROM grouped g
                LEFT JOIN operators op
                  ON lower(trim(op.name)) = lower(trim(g.name))
                  OR lower(trim(op.legal_name)) = lower(trim(g.name))
                ORDER BY lower(g.name)
                LIMIT %s OFFSET %s""",
            [*params, min(max(limit, 1), 100), max(offset, 0)],
        ).fetchall()
    return {
        "items": [
            {
                "name": row[0],
                "vertical": row[1],
                "signal_count": row[2],
                "opportunity_count": row[3],
                "id": row[4],
                "legal_name": row[5],
                "companies_house_number": row[6],
                "company_status": row[7],
                "incorporation_date": row[8],
                "companies_house_url": row[9],
                "resolution_outcome": row[10],
            }
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


def list_organisation_enrichment_candidates(
    settings: Settings, *, limit: int, vertical: str = "CHILDRENS_HOME"
) -> list[dict[str, Any]]:
    """Materialise and return bounded organisations with vertical activity.

    Early evidence often has a legitimate provider/employer name before an
    opportunity has resolved an ``operator_id``.  A manual enrichment run is
    the point at which those stored names become shared organisation
    candidates; requiring an existing opportunity link creates a circular
    dependency and leaves the first run empty.
    """
    vertical = validate_vertical(vertical)
    limit = min(max(limit, 1), 25)
    with connection(settings) as conn:
        rows = conn.execute(
            """WITH activity
               (name, companies_house_number, locality, postcode, address, website_url,
                provider_registered_name,
                provider_registered_locality, provider_registered_postcode,
                provider_registered_address, provider_registration_date,
                provider_evidence_at, priority)
               AS (
                 SELECT op.name, op.companies_house_number,
                        NULLIF(o.town, '') AS locality, NULLIF(o.postcode, ''),
                        NULLIF(o.address, ''), NULLIF(op.website_url, ''),
                        NULL::text, NULL::text, NULL::text, NULL::text, NULL::date,
                        NULL::timestamptz, 0 AS priority
                 FROM operators op
                 JOIN opportunities o ON o.operator_id = op.id
                 WHERE o.vertical = %s
                   AND o.review_status NOT IN ('MERGED', 'REJECTED')
                 UNION ALL
                 SELECT o.operator_name, NULL, NULLIF(o.town, ''),
                        NULLIF(o.postcode, ''), NULLIF(o.address, ''), NULL,
                        NULL::text, NULL::text, NULL::text, NULL::text, NULL::date,
                        NULL::timestamptz, 1
                 FROM opportunities o
                 WHERE o.vertical = %s
                   AND o.review_status NOT IN ('MERGED', 'REJECTED')
                   AND NULLIF(trim(o.operator_name), '') IS NOT NULL
                 UNION ALL
                 SELECT COALESCE(NULLIF(trim(se.operator_name), ''),
                                 NULLIF(trim(rs.organisation_hint), ''),
                                 NULLIF(trim(oe.registered_provider_name), '')),
                        COALESCE(rs.metadata->>'companies_house_number',
                                 rs.metadata->>'company_number'),
                        COALESCE(NULLIF(rs.metadata->>'town', ''),
                                 NULLIF(rs.metadata->>'locality', ''),
                                 NULLIF(rs.location_hint, '')),
                        NULLIF(rs.metadata->>'postcode', ''),
                        COALESCE(NULLIF(rs.metadata->>'address', ''),
                                 NULLIF(rs.location_hint, '')),
                        COALESCE(NULLIF(rs.metadata->>'website', ''),
                                 NULLIF(rs.metadata->>'website_url', '')),
                        NULLIF(oe.registered_provider_name, ''),
                        NULLIF(oe.provider_registered_locality, ''),
                        NULLIF(oe.provider_registered_postcode, ''),
                        NULLIF(oe.provider_registered_address, ''),
                        oe.registration_date,
                        oe.retrieved_at,
                        CASE WHEN rs.source_type = 'ofsted' THEN 2 ELSE 3 END
                 FROM raw_signals rs
                 LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                 LEFT JOIN LATERAL (
                   SELECT registered_provider_name, provider_registered_locality,
                          provider_registered_postcode, provider_registered_address,
                          registration_date, retrieved_at
                   FROM ofsted_urn_enrichments
                   WHERE raw_signal_id = rs.id
                   ORDER BY retrieved_at DESC, created_at DESC LIMIT 1
                 ) oe ON rs.source_type = 'ofsted'
                 WHERE rs.vertical = %s
                   AND COALESCE(NULLIF(trim(se.operator_name), ''),
                                NULLIF(trim(rs.organisation_hint), ''),
                                NULLIF(trim(oe.registered_provider_name), '')) IS NOT NULL
               )
               SELECT name, max(companies_house_number), max(locality), max(postcode),
                      max(address), max(website_url),
                      max(provider_registered_name),
                      max(provider_registered_locality),
                      max(provider_registered_postcode),
                      max(provider_registered_address), max(provider_registration_date),
                      max(provider_evidence_at),
                      min(priority)
               FROM activity
               WHERE lower(trim(name)) NOT IN ('unknown', 'not provided', 'redacted')
               GROUP BY lower(trim(name)), name
               ORDER BY min(priority), lower(trim(name))
               LIMIT %s""",
            (vertical, vertical, vertical, min(limit * 5, 125)),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for (
            name,
            company_number,
            locality,
            postcode,
            address,
            website_url,
            provider_name,
            provider_locality,
            provider_postcode,
            provider_address,
            provider_registration_date,
            provider_evidence_at,
            _priority,
        ) in rows:
            identity = normalize_identity(name)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            operator_id = _resolve_operator_id(
                conn,
                name,
                {"companies_house_number": company_number} if company_number else {},
            )
            operator = conn.execute(
                """SELECT name, companies_house_number, companies_house_refreshed_at,
                          EXISTS (
                            SELECT 1 FROM organisation_match_reviews
                            WHERE operator_id = operators.id
                              AND provider = 'COMPANIES_HOUSE' AND status = 'PENDING'
                          )
                   FROM operators WHERE id = %s""",
                (operator_id,),
            ).fetchone()
            if not operator:
                continue
            aliases = conn.execute(
                """SELECT alias FROM organisation_aliases
                   WHERE operator_id = %s ORDER BY created_at DESC LIMIT 10""",
                (operator_id,),
            ).fetchall()
            refreshed_at = operator[2]
            if (
                refreshed_at
                and refreshed_at > datetime.now(UTC) - timedelta(days=30)
                and (not provider_evidence_at or provider_evidence_at <= refreshed_at)
                and not (provider_name and len(operator) > 3 and operator[3])
            ):
                continue
            conn.execute(
                """UPDATE opportunities SET operator_id = %s, updated_at = now()
                   WHERE vertical = %s AND operator_id IS NULL
                     AND lower(trim(operator_name)) = lower(trim(%s))
                     AND review_status NOT IN ('MERGED', 'REJECTED')""",
                (operator_id, vertical, name),
            )
            candidates.append(
                {
                    "operator_id": str(operator_id),
                    "name": operator[0],
                    "company_number": operator[1],
                    "locality": locality,
                    "postcode": postcode,
                    "address": address,
                    "website": website_url,
                    "provider_registered_name": provider_name,
                    "provider_registered_locality": provider_locality,
                    "provider_registered_postcode": provider_postcode,
                    "provider_registered_address": provider_address,
                    "provider_registration_date": (
                        provider_registration_date.isoformat()
                        if provider_registration_date
                        else None
                    ),
                    "aliases": [
                        str(alias[0]).strip()
                        for alias in aliases
                        if str(alias[0] or "").strip()
                    ],
                }
            )
            if len(candidates) >= limit:
                break
        conn.commit()
    return candidates


def organisation_detail(settings: Settings, operator_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        operator = conn.execute(
            """SELECT id, name, legal_name, companies_house_number, website_url,
                      company_status, incorporation_date, company_type,
                      registered_office, sic_codes, companies_house_url,
                      companies_house_refreshed_at, resolution_outcome,
                      resolution_confidence, enrichment_provenance
               FROM operators WHERE id = %s""",
            (operator_id,),
        ).fetchone()
        if not operator:
            return None
        aliases = conn.execute(
            """SELECT alias, source, created_at FROM organisation_aliases
               WHERE operator_id = %s ORDER BY created_at""",
            (operator_id,),
        ).fetchall()
        opportunities = conn.execute(
            """SELECT id, name, vertical, change_type, lifecycle_stage, confidence
               FROM opportunities WHERE operator_id = %s
                 AND review_status NOT IN ('MERGED', 'REJECTED')
               ORDER BY latest_update_at DESC""",
            (operator_id,),
        ).fetchall()
        evidence = conn.execute(
            """SELECT provider, external_id, status, resolution_outcome,
                      resolution_confidence, reason, retrieved_at
               FROM organisation_evidence WHERE operator_id = %s
               ORDER BY retrieved_at DESC LIMIT 20""",
            (operator_id,),
        ).fetchall()
        ofsted_corroboration = conn.execute(
            """SELECT DISTINCT ON (urn) urn, registered_provider_name,
                      provision_type, registration_date, local_authority,
                      provider_registered_address, provider_registered_locality,
                      provider_registered_region, provider_registered_postcode,
                      latest_report_date, latest_report_publication_date,
                      latest_report_url, provider_page_url, retrieved_at
               FROM ofsted_urn_enrichments
               WHERE operator_id = %s
               ORDER BY urn, retrieved_at DESC, created_at DESC""",
            (operator_id,),
        ).fetchall()
    fields = (
        "id", "name", "legal_name", "companies_house_number", "website_url",
        "company_status", "incorporation_date", "company_type", "registered_office",
        "sic_codes", "companies_house_url", "companies_house_refreshed_at",
        "resolution_outcome", "resolution_confidence", "enrichment_provenance",
    )
    result = dict(zip(fields, operator))
    result["aliases"] = [
        {"alias": row[0], "source": row[1], "created_at": row[2]} for row in aliases
    ]
    result["opportunities"] = [
        {
            "id": row[0], "name": row[1], "vertical": row[2],
            "change_type": row[3], "lifecycle_stage": row[4], "confidence": row[5],
        }
        for row in opportunities
    ]
    result["evidence"] = [
        {
            "provider": row[0], "external_id": row[1], "status": row[2],
            "resolution_outcome": row[3], "resolution_confidence": row[4],
            "reason": row[5], "retrieved_at": row[6],
        }
        for row in evidence
    ]
    result["ofsted_corroboration"] = [
        dict(
            zip(
                (
                    "urn",
                    "registered_provider_name",
                    "provision_type",
                    "registration_date",
                    "local_authority",
                    "provider_registered_address",
                    "provider_registered_locality",
                    "provider_registered_region",
                    "provider_registered_postcode",
                    "latest_report_date",
                    "latest_report_publication_date",
                    "latest_report_url",
                    "provider_page_url",
                    "retrieved_at",
                ),
                row,
            )
        )
        for row in ofsted_corroboration
    ]
    return result


def list_organisation_match_reviews(
    settings: Settings, *, limit: int = 25
) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT r.id, r.operator_id, o.name, r.provider, r.query_name,
                      r.candidates, r.reason, r.created_at
               FROM organisation_match_reviews r
               JOIN operators o ON o.id = r.operator_id
               WHERE r.status = 'PENDING'
               ORDER BY r.created_at DESC LIMIT %s""",
            (min(max(limit, 1), 100),),
        ).fetchall()
        results = []
        for row in rows:
            operator = conn.execute(
                """SELECT name, legal_name, website_url
                   FROM operators WHERE id = %s""",
                (row[1],),
            ).fetchone()
            aliases = conn.execute(
                """SELECT alias, source FROM organisation_aliases
                   WHERE operator_id = %s ORDER BY created_at""",
                (row[1],),
            ).fetchall()
            identity_names = {
                str(value).strip().lower()
                for value in [
                    operator[0] if operator else None,
                    operator[1] if operator else None,
                    row[4],
                    *(alias[0] for alias in aliases),
                ]
                if str(value or "").strip()
            }
            opportunities = conn.execute(
                """SELECT id, name, vertical, town, postcode, address
                   FROM opportunities
                   WHERE operator_id = %s AND review_status NOT IN ('MERGED', 'REJECTED')
                   ORDER BY latest_update_at DESC LIMIT 20""",
                (row[1],),
            ).fetchall()
            signals = conn.execute(
                """SELECT DISTINCT rs.id, rs.title, rs.vertical, rs.source_type,
                          rs.source_url, rs.location_hint, rs.organisation_hint,
                          rs.metadata, rs.discovered_at
                   FROM raw_signals rs
                   LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   LEFT JOIN opportunity_signals os
                     ON os.raw_signal_id = rs.id AND os.status = 'ACTIVE'
                   LEFT JOIN opportunities opp ON opp.id = os.opportunity_id
                   WHERE opp.operator_id = %s
                      OR lower(trim(COALESCE(se.operator_name, rs.organisation_hint, '')))
                         = ANY(%s)
                   ORDER BY rs.discovered_at DESC LIMIT 30""",
                (row[1], list(identity_names)),
            ).fetchall()
            signal_ids = [signal[0] for signal in signals]
            ofsted_evidence = []
            if signal_ids:
                ofsted_evidence = conn.execute(
                    """SELECT DISTINCT ON (urn) urn, registered_provider_name,
                              provision_type, registration_date, local_authority,
                              provider_registered_address,
                              provider_registered_locality,
                              provider_registered_region,
                              provider_registered_postcode,
                              latest_report_date,
                              latest_report_publication_date,
                              latest_report_url, provider_page_url, retrieved_at
                       FROM ofsted_urn_enrichments
                       WHERE raw_signal_id = ANY(%s)
                       ORDER BY urn, retrieved_at DESC, created_at DESC""",
                    (signal_ids,),
                ).fetchall()
            ofsted_items = [
                dict(
                    zip(
                        (
                            "urn",
                            "registered_provider_name",
                            "provision_type",
                            "registration_date",
                            "local_authority",
                            "provider_registered_address",
                            "provider_registered_locality",
                            "provider_registered_region",
                            "provider_registered_postcode",
                            "latest_report_date",
                            "latest_report_publication_date",
                            "latest_report_url",
                            "provider_page_url",
                            "retrieved_at",
                        ),
                        evidence,
                    )
                )
                for evidence in ofsted_evidence
            ]
            candidates = row[5] or []
            if ofsted_items:
                evidence = ofsted_items[0]
                source_signal = signals[0] if signals else None
                comparison_context = OrganisationCandidate(
                    operator_id=str(row[1]),
                    name=str(row[4] or row[2]),
                    locality=(source_signal[7] or {}).get("town")
                    if source_signal
                    else None,
                    postcode=(source_signal[7] or {}).get("postcode")
                    if source_signal
                    else None,
                    provider_registered_name=evidence.get("registered_provider_name"),
                    provider_registered_locality=evidence.get(
                        "provider_registered_locality"
                    ),
                    provider_registered_postcode=evidence.get(
                        "provider_registered_postcode"
                    ),
                    provider_registered_address=evidence.get(
                        "provider_registered_address"
                    ),
                    provider_registration_date=(
                        str(evidence["registration_date"])
                        if evidence.get("registration_date")
                        else None
                    ),
                )
                candidates = [
                    {
                        **compare_company_candidate(comparison_context, candidate),
                        "selection_source": candidate.get(
                            "selection_source", "SUGGESTED"
                        ),
                    }
                    for candidate in candidates
                    if isinstance(candidate, dict)
                ]
                candidates.sort(
                    key=lambda candidate: (
                        1
                        if (candidate.get("location_agreement") or {}).get(
                            "ofsted_provider_postcode"
                        )
                        else 0,
                        float(candidate.get("name_similarity") or 0),
                    ),
                    reverse=True,
                )
            results.append(
                {
                    "id": row[0],
                    "operator_id": row[1],
                    "organisation_name": row[2],
                    "provider": row[3],
                    "query_name": row[4],
                    "candidates": candidates,
                    "reason": row[6],
                    "created_at": row[7],
                    "source_context": {
                        "observed_name": row[4],
                        "website": operator[2] if operator else None,
                        "aliases": [
                            {"alias": alias[0], "source": alias[1]} for alias in aliases
                        ],
                        "verticals": sorted(
                            {signal[2] for signal in signals}
                            | {opportunity[2] for opportunity in opportunities}
                        ),
                        "source_types": sorted({signal[3] for signal in signals}),
                        "signals": [
                            {
                                "id": signal[0],
                                "title": signal[1],
                                "vertical": signal[2],
                                "source_type": signal[3],
                                "source_url": signal[4],
                                "location": signal[5],
                                "organisation_name": signal[6],
                                "address": (signal[7] or {}).get("address"),
                                "postcode": (signal[7] or {}).get("postcode"),
                                "town": (signal[7] or {}).get("town")
                                or (signal[7] or {}).get("locality"),
                                "local_authority": (signal[7] or {}).get("council")
                                or (signal[7] or {}).get("local_authority"),
                                "discovered_at": signal[8],
                            }
                            for signal in signals
                        ],
                        "opportunities": [
                            {
                                "id": opportunity[0],
                                "name": opportunity[1],
                                "vertical": opportunity[2],
                                "town": opportunity[3],
                                "postcode": opportunity[4],
                                "address": opportunity[5],
                            }
                            for opportunity in opportunities
                        ],
                        "ofsted_evidence": ofsted_items,
                    },
                }
            )
    return results


def _organisation_candidate_fingerprint(candidates: list[dict[str, Any]]) -> str:
    import hashlib

    values = sorted(
        {
            str(candidate.get("company_number") or "").strip().upper()
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("company_number")
        }
    )
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def add_manual_organisation_candidate(
    settings: Settings,
    review_id: str,
    *,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Persist an official manual lookup result without resolving the review."""
    company_number = str(candidate.get("company_number") or "").strip().upper()
    if not company_number:
        raise ValueError("manual company candidate has no company number")
    manual_candidate = {**candidate, "selection_source": "MANUAL_LOOKUP"}
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT candidates, status FROM organisation_match_reviews
               WHERE id = %s FOR UPDATE""",
            (review_id,),
        ).fetchone()
        if not row:
            raise ValueError("organisation review not found")
        if row[1] != "PENDING":
            raise ValueError("organisation review is no longer pending")
        candidates = [
            item
            for item in (row[0] or [])
            if isinstance(item, dict)
            and str(item.get("company_number") or "").strip().upper() != company_number
        ]
        candidates.append(manual_candidate)
        conn.execute(
            """UPDATE organisation_match_reviews
               SET candidates = %s, candidate_fingerprint = %s
               WHERE id = %s""",
            (
                Jsonb(candidates),
                _organisation_candidate_fingerprint(candidates),
                review_id,
            ),
        )
        conn.commit()
    return manual_candidate


def resolve_organisation_match_review(
    settings: Settings,
    review_id: str,
    *,
    action: str,
    actor: str,
    company_number: str | None = None,
) -> dict[str, Any]:
    if action not in {"confirm", "reject"}:
        raise ValueError("unsupported organisation review action")
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT id, operator_id, candidates, status, query_name
               FROM organisation_match_reviews WHERE id = %s FOR UPDATE""",
            (review_id,),
        ).fetchone()
        if not row:
            raise ValueError("organisation review not found")
        if row[3] != "PENDING":
            return {"id": review_id, "status": row[3]}
        manual_selection = False
        if action == "confirm":
            candidates = row[2] or []
            selected = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict)
                    and str(item.get("company_number") or "") == str(company_number or "")
                ),
                None,
            )
            if not selected:
                raise ValueError("selected company is not a review candidate")
            manual_selection = selected.get("selection_source") == "MANUAL_LOOKUP"
            resolution_reason = (
                "administrator confirmed a manually entered Companies House number"
                if manual_selection
                else "administrator confirmed an ambiguous company match"
            )
            conn.execute(
                """UPDATE operators SET companies_house_number = %s, legal_name = %s,
                   company_status = %s, incorporation_date = %s,
                   company_type = %s, registered_office = %s, sic_codes = %s,
                   companies_house_url = %s, resolution_outcome = 'EXACT',
                   resolution_confidence = 1, enrichment_provenance = %s,
                   updated_at = now() WHERE id = %s""",
                (
                    selected.get("company_number"),
                    selected.get("company_name"),
                    selected.get("company_status"),
                    selected.get("date_of_creation") or None,
                    selected.get("type"),
                    Jsonb(selected.get("registered_office_address") or {}),
                    Jsonb(selected.get("sic_codes") or []),
                    selected.get("companies_house_url")
                    or (
                        "https://find-and-update.company-information.service.gov.uk/company/"
                        f"{selected.get('company_number')}"
                    ),
                    Jsonb(
                        {
                            "provider": "COMPANIES_HOUSE",
                            "reason": resolution_reason,
                            "review_id": review_id,
                            "selection_source": (
                                "MANUAL_COMPANY_NUMBER"
                                if manual_selection
                                else "SUGGESTED_CANDIDATE"
                            ),
                        }
                    ),
                    row[1],
                ),
            )
            for alias in {
                str(row[4] or "").strip(),
                str(selected.get("company_name") or "").strip(),
            }:
                normalized = normalize_identity(alias)
                if normalized:
                    conn.execute(
                        """INSERT INTO organisation_aliases
                           (operator_id, alias, normalized_alias, source)
                           VALUES (%s, %s, %s, 'ADMIN')
                           ON CONFLICT (operator_id, normalized_alias) DO NOTHING""",
                        (row[1], alias, normalized),
                    )
            status = "CONFIRMED"
        else:
            status = "REJECTED"
        candidates = row[2] or []
        conn.execute(
            """UPDATE organisation_match_reviews SET status = %s,
               reviewed_by = %s, reviewed_at = now(), candidate_fingerprint = %s
               WHERE id = %s""",
            (
                status,
                actor,
                _organisation_candidate_fingerprint(candidates),
                review_id,
            ),
        )
        conn.execute(
            """INSERT INTO admin_audit_events (action, actor, target_type, details)
               VALUES (%s, %s, 'organisation_match_review', %s)""",
            (
                (
                    "organisation_match_manual_company_confirm"
                    if action == "confirm" and manual_selection
                    else f"organisation_match_{action}"
                ),
                actor,
                Jsonb(
                    {
                        "review_id": review_id,
                        "operator_id": str(row[1]),
                        "company_number": company_number if action == "confirm" else None,
                    }
                ),
            ),
        )
        conn.commit()
    return {"id": review_id, "status": status, "operator_id": str(row[1])}


def opportunity_detail(settings: Settings, opportunity_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        opportunity = conn.execute(
            """SELECT id, name, operator_name, address, postcode, town, vertical, change_type,
                      event_type, lifecycle_stage, confidence,
                      confidence_breakdown, stage_reason, creation_reason, first_seen_at,
                      latest_update_at, location_sensitivity, operator_id,
                      publication_status, customer_title, customer_summary,
                      customer_published_by, customer_published_at
               FROM opportunities WHERE id = %s""",
            (opportunity_id,),
        ).fetchone()
        if not opportunity:
            return None
        rows = conn.execute(
            """SELECT rs.id, rs.source_type, rs.title, rs.external_id, rs.discovered_at,
                      rs.source_url, rs.metadata, rs.organisation_hint, rs.location_hint,
                      os.status, os.created_by, os.match_outcome, os.match_confidence,
                      os.match_reason, os.provenance, se.confidence, se.review_status,
                      ai.recommendation, ai.confidence, ai.status
               FROM opportunity_signals os JOIN raw_signals rs ON rs.id = os.raw_signal_id
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               LEFT JOIN LATERAL (SELECT recommendation, confidence, status FROM signal_ai_reviews
                 WHERE raw_signal_id = rs.id ORDER BY created_at DESC LIMIT 1) ai ON TRUE
               WHERE os.opportunity_id = %s ORDER BY rs.discovered_at""",
            (opportunity_id,),
        ).fetchall()
        organisation_evidence = []
        if opportunity[17]:
            organisation_evidence = conn.execute(
                """SELECT provider, external_id, status, resolution_outcome,
                          resolution_confidence, reason, retrieved_at
                   FROM organisation_evidence WHERE operator_id = %s
                   ORDER BY retrieved_at DESC LIMIT 10""",
                (opportunity[17],),
            ).fetchall()
    fields = (
        "id",
        "source_type",
        "title",
        "external_id",
        "discovered_at",
        "source_url",
        "metadata",
        "organisation_hint",
        "location_hint",
        "relationship_status",
        "relationship_created_by",
        "match_outcome",
        "match_confidence",
        "match_reason",
        "provenance",
        "rule_confidence",
        "review_status",
        "ai_recommendation",
        "ai_confidence",
        "ai_status",
    )
    return {
        "id": opportunity[0],
        "name": opportunity[1],
        "operator_name": opportunity[2],
        "address": opportunity[3],
        "postcode": opportunity[4],
        "town": opportunity[5],
        "vertical": opportunity[6],
        "change_type": opportunity[7],
        "event_type": opportunity[8],
        "lifecycle_stage": opportunity[9],
        "confidence": opportunity[10],
        "confidence_breakdown": opportunity[11],
        "stage_reason": opportunity[12],
        "creation_reason": opportunity[13],
        "first_seen_at": opportunity[14],
        "latest_update_at": opportunity[15],
        "location_sensitivity": opportunity[16],
        "operator_id": opportunity[17],
        "publication_status": opportunity[18],
        "customer_title": opportunity[19],
        "customer_summary": opportunity[20],
        "customer_published_by": opportunity[21],
        "customer_published_at": opportunity[22],
        "signals": [dict(zip(fields, row)) for row in rows],
        "organisation_evidence": [
            {
                "provider": row[0], "external_id": row[1], "status": row[2],
                "resolution_outcome": row[3], "resolution_confidence": row[4],
                "reason": row[5], "retrieved_at": row[6],
            }
            for row in organisation_evidence
        ],
    }


def _cleanup_match_reviews(
    settings: Settings, *, limit: int, actor: str
) -> dict[str, int]:
    """Close bounded review suggestions invalidated by consolidation or linking."""
    closed_superseded = 0
    closed_linked = 0
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, raw_signal_id, opportunity_id
               FROM opportunity_match_reviews
               WHERE status = 'PENDING'
               ORDER BY created_at, id
               LIMIT %s
               FOR UPDATE""",
            (min(max(limit, 1), 100),),
        ).fetchall()
        for review_id, signal_id, opportunity_id in rows:
            canonical = _canonical_opportunity_id(conn, opportunity_id)
            if canonical is None or canonical != opportunity_id:
                reason = "candidate opportunity superseded by consolidation"
                status = "SUPERSEDED"
                closed_superseded += 1
            elif conn.execute(
                """SELECT 1 FROM opportunity_signals
                   WHERE raw_signal_id = %s AND status = 'ACTIVE'""",
                (signal_id,),
            ).fetchone():
                reason = "signal already linked to canonical opportunity"
                status = "SUPERSEDED"
                closed_linked += 1
            else:
                continue
            conn.execute(
                """UPDATE opportunity_match_reviews
                   SET status = %s, reason = %s, reviewed_by = %s, reviewed_at = now()
                   WHERE id = %s AND status = 'PENDING'""",
                (status, reason, actor, review_id),
            )
        conn.commit()
    if closed_superseded or closed_linked:
        record_admin_audit(
            settings,
            action="match_review_cleanup",
            actor=actor,
            target_type="opportunity_match_review",
            details={
                "bounded": True,
                "closed_superseded": closed_superseded,
                "closed_linked": closed_linked,
            },
        )
    return {"closed_superseded": closed_superseded, "closed_linked": closed_linked}


def list_match_reviews(
    settings: Settings, *, limit: int, offset: int, vertical: str | None = None
) -> dict[str, Any]:
    vertical = validate_vertical_filter(vertical)
    vertical_clause = ""
    params: list[Any] = []
    if vertical != ALL_VERTICALS:
        vertical_clause = " AND mr.vertical = %s"
        params.append(vertical)
    with connection(settings) as conn:
        total = conn.execute(
            f"""SELECT count(*)
               FROM opportunity_match_reviews mr
               JOIN opportunities o ON o.id = mr.opportunity_id
               WHERE mr.status = 'PENDING'
                 AND o.review_status NOT IN ('MERGED', 'REJECTED')
                 {vertical_clause}
                 AND NOT EXISTS (
                     SELECT 1 FROM opportunity_signals os
                     WHERE os.raw_signal_id = mr.raw_signal_id AND os.status = 'ACTIVE'
                 )""",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT mr.id, mr.raw_signal_id, mr.opportunity_id, mr.outcome,
                      mr.confidence, mr.reason, mr.created_at,
                      rs.title, rs.source_type, o.name, mr.vertical
               FROM opportunity_match_reviews mr
               JOIN raw_signals rs ON rs.id = mr.raw_signal_id
               JOIN opportunities o ON o.id = mr.opportunity_id
               WHERE mr.status = 'PENDING'
                 AND o.review_status NOT IN ('MERGED', 'REJECTED')
                 {vertical_clause}
                 AND NOT EXISTS (
                     SELECT 1 FROM opportunity_signals os
                     WHERE os.raw_signal_id = mr.raw_signal_id AND os.status = 'ACTIVE'
                 )
               ORDER BY mr.created_at DESC LIMIT %s OFFSET %s""",
            (*params, limit, offset),
        ).fetchall()
    fields = (
        "id",
        "signal_id",
        "opportunity_id",
        "outcome",
        "confidence",
        "reason",
        "created_at",
        "signal_title",
        "source_type",
        "opportunity_name",
        "vertical",
    )
    return {
        "items": [dict(zip(fields, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _audit_match(settings: Settings, action: str, actor: str, details: dict[str, Any]) -> None:
    record_admin_audit(
        settings, action=action, actor=actor, target_type="opportunity_match", details=details
    )


def _archive_relationship(
    conn: Any, opportunity_id: str, signal_id: str, action: str, actor: str, reason: str | None
) -> None:
    conn.execute(
        """INSERT INTO opportunity_signal_history
           (opportunity_id, raw_signal_id, status, action, actor, reason)
           SELECT opportunity_id, raw_signal_id, status, %s, %s, %s
           FROM opportunity_signals
           WHERE opportunity_id = %s AND raw_signal_id = %s""",
        (action, actor, reason, opportunity_id, signal_id),
    )


def _signal_summary(conn: Any, signal_id: str) -> tuple[Any, ...] | None:
    return conn.execute(
        """SELECT rs.title, rs.raw_text, rs.source_type, rs.metadata, rs.organisation_hint,
                  rs.location_hint, COALESCE(se.nursery_name, se.operator_name),
                  se.event_type, se.lifecycle_stage, se.confidence, se.extracted_facts,
                  rs.vertical
           FROM raw_signals rs LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
           WHERE rs.id = %s""",
        (signal_id,),
    ).fetchone()


def create_opportunity_from_signal(
    settings: Settings, signal_id: str, actor: str
) -> dict[str, Any]:
    with connection(settings) as conn:
        signal = _signal_summary(conn, signal_id)
        if signal is None:
            raise ValueError("signal_not_found")
        (
            title,
            raw_text,
            source_type,
            metadata,
            organisation,
            location,
            name,
            event_type,
            lifecycle,
            confidence,
            facts,
            vertical,
        ) = signal
        metadata = metadata or {}
        existing = conn.execute(
            """SELECT o.id FROM opportunities o JOIN opportunity_signals os
               ON os.opportunity_id = o.id WHERE os.raw_signal_id = %s AND os.status = 'ACTIVE'""",
            (signal_id,),
        ).fetchone()
        if existing:
            return {"opportunity_id": str(existing[0]), "created": False}
        facts = facts or {}
        candidate = {
            "source_type": source_type,
            "raw_text": raw_text,
            "nursery_name": name,
            "operator_name": organisation,
            "address": location,
            "event_type": event_type,
            "extracted_facts": facts,
            "metadata": metadata,
        }
        vertical_policy = policy_for(str(vertical))
        creation = vertical_policy.determine_opportunity_action(candidate)
        operator_id = _resolve_operator_id(conn, organisation, metadata)
        opportunity_id = conn.execute(
            """INSERT INTO opportunities
               (name, operator_id, event_type, lifecycle_stage, confidence, vertical, operator_name,
                address, postcode, town, stage_reason, creation_reason, change_type,
                location_sensitivity)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                vertical_policy.build_opportunity_title(candidate, creation),
                operator_id,
                event_type or "other",
                lifecycle or "DISCOVERED",
                confidence or 0,
                vertical,
                organisation,
                location,
                metadata.get("postcode"),
                metadata.get("town") or metadata.get("locality"),
                "created by admin from signal",
                "Created manually from preserved signal evidence.",
                creation.change_type,
                "INTERNAL_EXACT" if vertical == "CHILDRENS_HOME" else "STANDARD",
            ),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO opportunity_signals
               (opportunity_id, raw_signal_id, relationship_type, provenance, status,
                created_by, match_outcome, match_confidence, match_reason)
               VALUES (%s, %s, 'SUPPORTS', %s, 'ACTIVE', 'ADMIN', 'EXACT', %s, %s)""",
            (
                opportunity_id,
                signal_id,
                Jsonb({"method": "admin", "reason": "created from signal"}),
                confidence or 0,
                "admin-created opportunity",
            ),
        )
        conn.commit()
    _audit_match(
        settings,
        "opportunity_created_from_signal",
        actor,
        {"signal_id": signal_id, "opportunity_id": str(opportunity_id)},
    )
    return {"opportunity_id": str(opportunity_id), "created": True}


def link_signal_to_opportunity(
    settings: Settings, opportunity_id: str, signal_id: str, actor: str, reason: str = "admin link"
) -> dict[str, Any]:
    with connection(settings) as conn:
        opportunity = conn.execute(
            "SELECT vertical, review_status FROM opportunities WHERE id = %s", (opportunity_id,)
        ).fetchone()
        if not opportunity:
            raise ValueError("opportunity_not_found")
        signal = _signal_summary(conn, signal_id)
        if signal is None:
            raise ValueError("signal_not_found")
        if opportunity[0] != signal[-1]:
            raise ValueError("signal and opportunity verticals must match")
        if opportunity[1] in {"MERGED", "REJECTED"}:
            raise ValueError("opportunity_is_not_current")
        confidence = signal[9] or 0
        row = conn.execute(
            "SELECT status FROM opportunity_signals "
            "WHERE opportunity_id = %s AND raw_signal_id = %s",
            (opportunity_id, signal_id),
        ).fetchone()
        if row:
            _archive_relationship(conn, opportunity_id, signal_id, "ADMIN_LINK", actor, reason)
            conn.execute(
                """UPDATE opportunity_signals SET status = 'ACTIVE', created_by = 'ADMIN',
                   match_outcome = 'EXACT', match_confidence = %s, match_reason = %s,
                   admin_override_by = %s, admin_override_at = now()
                   WHERE opportunity_id = %s AND raw_signal_id = %s""",
                (confidence, reason, actor, opportunity_id, signal_id),
            )
        else:
            conn.execute(
                """INSERT INTO opportunity_signals
                   (opportunity_id, raw_signal_id, relationship_type, provenance, status,
                    created_by, match_outcome, match_confidence, match_reason,
                    admin_override_by, admin_override_at)
                   VALUES (%s, %s, 'SUPPORTS', %s, 'ACTIVE', 'ADMIN', 'EXACT', %s, %s, %s,
                           now())""",
                (
                    opportunity_id,
                    signal_id,
                    Jsonb({"method": "admin", "reason": reason}),
                    confidence,
                    reason,
                    actor,
                ),
            )
        if signal[2] == "ofsted":
            conn.execute(
                """UPDATE opportunities SET lifecycle_stage = 'REGISTRATION',
                   stage_reason = 'Ofsted evidence confirmed by an administrator',
                   latest_update_at = now(), updated_at = now() WHERE id = %s""",
                (opportunity_id,),
            )
        else:
            conn.execute(
                """UPDATE opportunities SET latest_update_at = now(), updated_at = now()
                   WHERE id = %s""",
                (opportunity_id,),
            )
        conn.commit()
    _audit_match(
        settings,
        "opportunity_signal_linked",
        actor,
        {"signal_id": signal_id, "opportunity_id": opportunity_id, "reason": reason},
    )
    return {"opportunity_id": opportunity_id, "signal_id": signal_id, "status": "ACTIVE"}


def unlink_signal_from_opportunity(
    settings: Settings,
    opportunity_id: str,
    signal_id: str,
    actor: str,
    reason: str = "admin unlink",
) -> bool:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT status FROM opportunity_signals "
            "WHERE opportunity_id = %s AND raw_signal_id = %s",
            (opportunity_id, signal_id),
        ).fetchone()
        if not row:
            return False
        _archive_relationship(conn, opportunity_id, signal_id, "ADMIN_UNLINK", actor, reason)
        conn.execute(
            """UPDATE opportunity_signals SET status = 'REJECTED', admin_override_by = %s,
               admin_override_at = now(), match_reason = %s
               WHERE opportunity_id = %s AND raw_signal_id = %s""",
            (actor, reason, opportunity_id, signal_id),
        )
        conn.commit()
    _audit_match(
        settings,
        "opportunity_signal_unlinked",
        actor,
        {"signal_id": signal_id, "opportunity_id": opportunity_id, "reason": reason},
    )
    return True


def merge_opportunities(
    settings: Settings, source_id: str, target_id: str, actor: str
) -> dict[str, Any]:
    if source_id == target_id:
        raise ValueError("cannot_merge_opportunity_into_itself")
    with connection(settings) as conn:
        source = conn.execute(
            "SELECT vertical, review_status FROM opportunities WHERE id = %s", (source_id,)
        ).fetchone()
        if not source:
            raise ValueError("source_opportunity_not_found")
        target = conn.execute(
            "SELECT vertical, review_status FROM opportunities WHERE id = %s", (target_id,)
        ).fetchone()
        if not target:
            raise ValueError("target_opportunity_not_found")
        if source[0] != target[0]:
            raise ValueError("opportunities_must_share_vertical")
        if source[1] in {"MERGED", "REJECTED"} or target[1] in {"MERGED", "REJECTED"}:
            raise ValueError("opportunity_is_not_current")
        links = conn.execute(
            "SELECT raw_signal_id FROM opportunity_signals "
            "WHERE opportunity_id = %s AND status = 'ACTIVE'",
            (source_id,),
        ).fetchall()
        for (signal_id,) in links:
            _archive_relationship(
                conn, source_id, signal_id, "ADMIN_MERGE", actor, f"merged into {target_id}"
            )
            conn.execute(
                """UPDATE opportunity_signals SET status = 'REJECTED',
                admin_override_by = %s, admin_override_at = now(), match_reason = %s
                WHERE opportunity_id = %s AND raw_signal_id = %s""",
                (actor, f"merged into {target_id}", source_id, signal_id),
            )
            conn.execute(
                """INSERT INTO opportunity_signals
                   (opportunity_id, raw_signal_id, relationship_type, provenance, status,
                    created_by, match_outcome, match_confidence, match_reason,
                    admin_override_by, admin_override_at)
                   VALUES (%s, %s, 'SUPPORTS', %s, 'ACTIVE', 'ADMIN', 'STRONG', 1, %s, %s, now())
                   ON CONFLICT (opportunity_id, raw_signal_id) DO UPDATE SET status = 'ACTIVE',
                    created_by = 'ADMIN', admin_override_by = EXCLUDED.admin_override_by,
                    admin_override_at = now(), match_reason = EXCLUDED.match_reason""",
                (
                    target_id,
                    signal_id,
                    Jsonb({"method": "admin_merge", "from": source_id}),
                    f"merged from {source_id}",
                    actor,
                ),
            )
        conn.execute(
            """UPDATE opportunities SET review_status = 'MERGED',
            merged_into_opportunity_id = %s, updated_at = now() WHERE id = %s""",
            (target_id, source_id),
        )
        conn.commit()
    _audit_match(
        settings,
        "opportunities_merged",
        actor,
        {
            "source_opportunity_id": source_id,
            "target_opportunity_id": target_id,
            "moved_signals": len(links),
        },
    )
    return {
        "source_opportunity_id": source_id,
        "target_opportunity_id": target_id,
        "moved_signals": len(links),
    }


def split_opportunity(
    settings: Settings,
    opportunity_id: str,
    signal_ids: list[str],
    actor: str,
    name: str | None = None,
) -> dict[str, Any]:
    if not signal_ids:
        raise ValueError("signal_ids_required")
    with connection(settings) as conn:
        base = conn.execute(
            "SELECT name, event_type, lifecycle_stage, confidence, vertical, operator_name, "
            "address, postcode, town, change_type, creation_reason "
            "FROM opportunities WHERE id = %s",
            (opportunity_id,),
        ).fetchone()
        if not base:
            raise ValueError("opportunity_not_found")
        new_id = conn.execute(
            """INSERT INTO opportunities
               (name, event_type, lifecycle_stage, confidence, vertical, operator_name,
                address, postcode, town, change_type, creation_reason, stage_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       'created by admin split') RETURNING id""",
            (name or f"{base[0]} (split)", *base[1:]),
        ).fetchone()[0]
        moved = 0
        for signal_id in signal_ids:
            if not conn.execute(
                """SELECT 1 FROM opportunity_signals WHERE opportunity_id = %s
                                  AND raw_signal_id = %s AND status = 'ACTIVE'""",
                (opportunity_id, signal_id),
            ).fetchone():
                continue
            _archive_relationship(
                conn, opportunity_id, signal_id, "ADMIN_SPLIT", actor, f"split into {new_id}"
            )
            conn.execute(
                """UPDATE opportunity_signals SET status = 'REJECTED',
                admin_override_by = %s, admin_override_at = now(), match_reason = %s
                WHERE opportunity_id = %s AND raw_signal_id = %s""",
                (actor, f"split into {new_id}", opportunity_id, signal_id),
            )
            conn.execute(
                """INSERT INTO opportunity_signals
                (opportunity_id, raw_signal_id, relationship_type, provenance, status,
                 created_by, match_outcome, match_confidence, match_reason,
                 admin_override_by, admin_override_at)
                VALUES (%s, %s, 'SUPPORTS', %s, 'ACTIVE', 'ADMIN', 'STRONG', 1, %s, %s, now())""",
                (
                    new_id,
                    signal_id,
                    Jsonb({"method": "admin_split", "from": opportunity_id}),
                    f"split from {opportunity_id}",
                    actor,
                ),
            )
            moved += 1
        conn.commit()
    _audit_match(
        settings,
        "opportunity_split",
        actor,
        {
            "source_opportunity_id": opportunity_id,
            "new_opportunity_id": str(new_id),
            "signal_count": moved,
        },
    )
    return {"opportunity_id": str(new_id), "moved_signals": moved}


def resolve_match_review(
    settings: Settings, review_id: str, action: str, actor: str
) -> dict[str, Any]:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT raw_signal_id, opportunity_id, status FROM opportunity_match_reviews "
            "WHERE id = %s",
            (review_id,),
        ).fetchone()
        if not row:
            raise ValueError("match_review_not_found")
        if row[2] != "PENDING":
            return {"review_id": review_id, "status": row[2]}
        status = "LINKED" if action == "link" else "REJECTED"
        conn.execute(
            """UPDATE opportunity_match_reviews SET status = %s,
            reviewed_by = %s, reviewed_at = now() WHERE id = %s""",
            (status, actor, review_id),
        )
        conn.commit()
    if action == "link":
        link_signal_to_opportunity(
            settings, str(row[1]), str(row[0]), actor, "match review accepted"
        )
    _audit_match(
        settings,
        "match_review_resolved",
        actor,
        {
            "review_id": review_id,
            "action": action,
            "signal_id": str(row[0]),
            "opportunity_id": str(row[1]),
        },
    )
    return {
        "review_id": review_id,
        "status": status,
        "signal_id": str(row[0]),
        "opportunity_id": str(row[1]),
    }


def ai_review_exists(
    settings: Settings, signal_id: str, model_id: str, prompt_version: str
) -> bool:
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT 1 FROM signal_ai_reviews
               WHERE raw_signal_id = %s AND provider = 'BEDROCK'
                 AND model_id = %s AND prompt_version = %s LIMIT 1""",
            (signal_id, model_id, prompt_version),
        ).fetchone()
    return row is not None


def get_ai_review(
    settings: Settings, signal_id: str, model_id: str, prompt_version: str
) -> dict[str, Any] | None:
    """Return the versioned shadow result for a signal, if it exists."""
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT id, provider, model_id, prompt_version, recommendation, confidence,
                      reason, recruitment_relevance, planning_relevance, commercial_change_evidence,
                      status, failure_category,
                      attempted_at, evaluated_at,
                      input_tokens, output_tokens, latency_ms, created_at
               FROM signal_ai_reviews
               WHERE raw_signal_id = %s AND provider = 'BEDROCK'
                 AND model_id = %s AND prompt_version = %s
               ORDER BY created_at DESC LIMIT 1""",
            (signal_id, model_id, prompt_version),
        ).fetchone()
    if row is None:
        return None
    return dict(
        zip(
            (
                "id",
                "provider",
                "model_id",
                "prompt_version",
                "recommendation",
                "confidence",
                "reason",
                "recruitment_relevance",
                "planning_relevance",
                "commercial_change_evidence",
                "status",
                "failure_category",
                "attempted_at",
                "evaluated_at",
                "input_tokens",
                "output_tokens",
                "latency_ms",
                "created_at",
            ),
            row,
        )
    )


def get_signal_review_status(settings: Settings, signal_id: str) -> str | None:
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT review_status FROM signal_enrichments WHERE raw_signal_id = %s",
            (signal_id,),
        ).fetchone()
    return row[0] if row else None


def save_ai_review(settings: Settings, signal_id: str, review: dict[str, Any]) -> bool:
    with connection(settings) as conn:
        row = conn.execute(
            """INSERT INTO signal_ai_reviews (
                raw_signal_id, vertical, provider, model_id, prompt_version, recommendation,
                confidence, reason, recruitment_relevance, planning_relevance,
                commercial_change_evidence, status, failure_category, attempted_at,
                evaluated_at, input_tokens, output_tokens, latency_ms
            ) VALUES (%s, (SELECT vertical FROM raw_signals WHERE id = %s), %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s,
                      to_timestamp(%s), to_timestamp(%s), %s, %s, %s)
            ON CONFLICT (raw_signal_id, provider, model_id, prompt_version) DO NOTHING
            RETURNING id""",
            (
                signal_id,
                signal_id,
                review["provider"],
                review["model_id"],
                review["prompt_version"],
                review.get("recommendation"),
                review.get("confidence"),
                review.get("reason"),
                review.get("recruitment_relevance"),
                review.get("planning_relevance"),
                review.get("commercial_change_evidence"),
                review["status"],
                review.get("failure_category"),
                review["attempted_at"],
                review.get("evaluated_at"),
                review.get("input_tokens"),
                review.get("output_tokens"),
                review.get("latency_ms"),
            ),
        ).fetchone()
        conn.commit()
    return row is not None


def _identity(row: tuple[Any, ...]) -> SignalIdentity:
    return SignalIdentity(
        id=row[0],
        evidence_key=row[1],
        enrichment_queued_at=row[2],
        content_sha256=row[3],
    )
