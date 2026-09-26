from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.classification import CLASSIFICATION_RULE_VERSION
from app.config import Settings
from app.correlation import classify_match, recruitment_evidence_strength
from app.db import connection
from app.enrichment import fixture_enrichment
from app.ingestion import NormalizedSignal
from app.opportunity_policy import opportunity_creation_decision, opportunity_title
from app.queueing import EnrichmentMessage, send_enrichment_message
from app.recruitment import recruitment_record_from_signal


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


def find_signal(settings: Settings, source_type: str, external_id: str) -> SignalIdentity | None:
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, sd.s3_key, rs.enrichment_queued_at, rs.content_sha256
            FROM raw_signals rs
            LEFT JOIN source_documents sd ON sd.raw_signal_id = rs.id
            WHERE rs.source_type = %s AND rs.external_id = %s
            ORDER BY sd.captured_at DESC NULLS LAST
            LIMIT 1
            """,
            (source_type, external_id),
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
                schema_version, source_type, source_url, external_id, discovered_at,
                title, raw_text, location_hint, organisation_hint, metadata, content_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_type, external_id) DO NOTHING
            RETURNING id, enrichment_queued_at, content_sha256
            """,
            (
                signal.schema_version,
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
            ),
        ).fetchone()
        created = row is not None
        if row is None:
            row = conn.execute(
                """
                SELECT rs.id, rs.enrichment_queued_at, rs.content_sha256
                FROM raw_signals rs
                WHERE rs.source_type = %s AND rs.external_id = %s
                """,
                (signal.source_type, signal.external_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("signal disappeared during idempotent insert")
        signal_id, queued_at, stored_hash = row
        conn.execute(
            """
            INSERT INTO source_documents (raw_signal_id, s3_bucket, s3_key, sha256, mime_type)
            VALUES (%s, %s, %s, %s, 'application/json')
            ON CONFLICT (s3_bucket, s3_key) DO NOTHING
            """,
            (signal_id, bucket, key, content_sha256),
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
                raw_signal_id, content_sha256, source_url, observed_at,
                planning_status, decision, metadata, evidence_bucket, evidence_key
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (raw_signal_id, content_sha256) DO NOTHING
            RETURNING id
            """,
            (
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
            INSERT INTO source_documents (raw_signal_id, s3_bucket, s3_key, sha256, mime_type)
            VALUES (%s, %s, %s, %s, 'application/json')
            ON CONFLICT (s3_bucket, s3_key) DO NOTHING
            """,
            (signal_id, bucket, key, content_sha256),
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
        )
        send_enrichment_message(settings, message)
        conn.execute(
            "UPDATE raw_signals SET enrichment_queued_at = now() WHERE id = %s",
            (identity.id,),
        )
        conn.commit()
    return True


def get_raw_signal(settings: Settings, signal_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT id, schema_version, source_type, source_url, external_id, discovered_at,
                   title, raw_text, location_hint, organisation_hint, metadata
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
    )
    return dict(zip(keys, row))


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
) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    if review_status:
        if review_status == "REVIEWED":
            clauses.append("se.review_status IN ('APPROVED', 'REJECTED')")
        else:
            clauses.append("se.review_status = %s")
            params.append(review_status)
    if source_type:
        clauses.append("rs.source_type = %s")
        params.append(source_type)
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
                   rs.metadata, rs.created_at, rs.enrichment_queued_at, se.review_status,
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
                   rs.location_hint, rs.organisation_hint, rs.metadata,
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
                        "review_status",
                    ),
                    row,
                )
            )
            if raw["review_status"] is None:
                without_enrichment += 1
                continue
            candidate = fixture_enrichment(raw)
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
                   rs.location_hint, rs.organisation_hint, rs.metadata,
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
            candidate = fixture_enrichment(raw)
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
                raw_signal_id, schema_version, event_type, nursery_name, operator_name, address,
                expected_opening_date, capacity, lifecycle_stage, confidence,
                extracted_facts, evidence, review_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
            ON CONFLICT (raw_signal_id) DO NOTHING
            RETURNING id
            """,
            (
                candidate["raw_signal_id"],
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
        conn.commit()
    return row is not None


def correlate_signal(
    settings: Settings, signal_id: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    """Create/link an opportunity only when the source supports that decision."""
    creation = opportunity_creation_decision(candidate)
    if creation.decision in {"IGNORE_FOR_OPPORTUNITY", "REVIEW"}:
        return {
            "opportunity_id": None,
            "linked": False,
            "created": False,
            "creation_decision": creation.decision,
            "reason": creation.reason,
        }
    metadata = candidate.get("metadata") or {}
    postcode = metadata.get("postcode")
    name = (
        candidate.get("nursery_name")
        or candidate.get("operator_name")
        or candidate.get("address")
        or "Unmatched nursery signal"
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
        existing = conn.execute(
            """SELECT o.id, o.name, o.operator_id, o.lifecycle_stage, o.confidence,
                      o.confidence_breakdown, COALESCE(n.postcode, linked.postcode),
                      COALESCE(op.name, linked.operator_name)
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
                 ORDER BY o.updated_at DESC"""
        ).fetchall()
        match = None
        match_reason = None
        uncertain_matches: list[tuple[Any, float, str]] = []
        for row in existing:
            comparison = classify_match(postcode, name, row[6], row[1])
            operator_comparison = classify_match(postcode, operator, row[6], row[7])
            if comparison.outcome == "UNCERTAIN" or operator_comparison.outcome == "UNCERTAIN":
                uncertain = comparison if comparison.outcome == "UNCERTAIN" else operator_comparison
                uncertain_matches.append((row[0], uncertain.confidence, uncertain.reason))
            if comparison.outcome == "EXACT" or operator_comparison.outcome == "EXACT":
                blocked = conn.execute(
                    """SELECT 1 FROM opportunity_signals
                       WHERE opportunity_id = %s AND raw_signal_id = %s AND status = 'REJECTED'""",
                    (row[0], signal_id),
                ).fetchone()
                if blocked:
                    continue
                match = row
                match_reason = (
                    comparison.reason if comparison.matched else operator_comparison.reason
                )
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
                "STAFFING"
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
                    else match_reason,
                    opportunity_id,
                ),
            )
        elif creation.decision == "CREATE_OPPORTUNITY":
            display_name = opportunity_title(candidate, creation)
            opportunity_id = conn.execute(
                """INSERT INTO opportunities
                   (name, event_type, lifecycle_stage, confidence, confidence_breakdown,
                    stage_reason, vertical, operator_name, address, postcode, town, change_type)
                   VALUES (%s, %s, %s, %s, %s, %s, 'NURSERY', %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    display_name,
                    candidate.get("event_type") or "other",
                    "PLANNING" if source_type == "planning" else "DISCOVERED",
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
                    operator,
                    candidate.get("address"),
                    postcode,
                    metadata.get("town") or metadata.get("locality"),
                    creation.change_type,
                ),
            ).fetchone()[0]
        else:
            conn.commit()
            return {
                "opportunity_id": None,
                "linked": False,
                "created": False,
                "creation_decision": creation.decision,
                "reason": creation.reason,
            }
        conn.execute(
            """INSERT INTO opportunity_signals (
                   opportunity_id, raw_signal_id, relationship_type,
                   extracted_facts, provenance, status, created_by,
                   match_outcome, match_confidence, match_reason)
               VALUES (%s, %s, 'SUPPORTS', %s, %s, 'ACTIVE', 'SYSTEM', %s, %s, %s)
               ON CONFLICT (opportunity_id, raw_signal_id) DO NOTHING""",
            (
                opportunity_id,
                signal_id,
                Jsonb(candidate.get("extracted_facts") or {}),
                Jsonb({"method": "deterministic-v1", "reason": match_reason or "initial signal"}),
                "STRONG" if match else "NO_MATCH",
                base_confidence,
                match_reason or "initial signal",
            ),
        )
        for possible_id, possible_confidence, possible_reason in uncertain_matches[:3]:
            conn.execute(
                """INSERT INTO opportunity_match_reviews
                   (raw_signal_id, opportunity_id, outcome, confidence, reason)
                   VALUES (%s, %s, 'UNCERTAIN', %s, %s)
                   ON CONFLICT (raw_signal_id, opportunity_id) DO NOTHING""",
                (signal_id, possible_id, possible_confidence, possible_reason),
            )
        conn.commit()
    return {
        "opportunity_id": str(opportunity_id),
        "linked": bool(match),
        "created": not bool(match),
        "creation_decision": creation.decision,
        "reason": match_reason or "initial signal",
    }


def recalculate_opportunity_creation(
    settings: Settings, *, actor: str, limit: int = 100, signal_ids: list[str] | None = None
) -> dict[str, Any]:
    """Boundedly recalculate opportunity creation from stored signal evidence."""
    limit = min(max(limit, 1), 100)
    with connection(settings) as conn:
        params: list[Any] = []
        where = "rs.source_type IN ('planning', 'recruitment')"
        if signal_ids:
            where += " AND rs.id = ANY(%s::uuid[])"
            params.append(signal_ids[:limit])
        rows = conn.execute(
            f"""SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url, rs.external_id,
                       rs.discovered_at, rs.title, rs.raw_text, rs.location_hint,
                       rs.organisation_hint, rs.metadata, se.review_status
                FROM raw_signals rs LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                WHERE {where} ORDER BY rs.discovered_at DESC LIMIT %s""",
            [*params, limit],
        ).fetchall()
    selected = 0
    created = 0
    linked = 0
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
                    "review_status",
                ),
                row,
            )
        )
        candidate = fixture_enrichment(raw)
        facts = candidate["extracted_facts"]
        creation = opportunity_creation_decision(candidate)
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
                                     WHERE h.opportunity_id = o.id AND h.action LIKE 'ADMIN%')""",
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
                             WHERE h.opportunity_id = opportunities.id AND h.action LIKE 'ADMIN%'
                         )""",
                    (opportunity_title(candidate, creation), creation.change_type, raw["id"]),
                )
            conn.commit()
        result = correlate_signal(settings, str(raw["id"]), candidate)
        if result.get("created"):
            created += 1
        elif result.get("linked"):
            linked += 1
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
            "created": created,
            "linked": linked,
            "routine_only_demoted": routine_only_demoted,
            "review_required": review_required,
            "unchanged": unchanged,
            "bounded": True,
        },
    )
    return {
        "operation_id": operation,
        "selected": selected,
        "created": created,
        "linked": linked,
        "routine_only_demoted": routine_only_demoted,
        "review_required": review_required,
        "unchanged": unchanged,
    }


def list_opportunities(
    settings: Settings, *, limit: int, offset: int, search: str | None = None
) -> dict[str, Any]:
    clauses = ["TRUE"]
    params: list[Any] = []
    if search:
        clauses.append("(o.name ILIKE %s OR COALESCE(o.stage_reason, '') ILIKE %s)")
        pattern = f"%{search[:200]}%"
        params.extend([pattern, pattern])
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
                       o.confidence_breakdown, o.stage_reason, o.first_seen_at, o.latest_update_at,
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
        "first_seen_at",
        "latest_update_at",
        "signal_count",
    )
    return {
        "items": [dict(zip(fields, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def opportunity_detail(settings: Settings, opportunity_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        opportunity = conn.execute(
            """SELECT id, name, operator_name, address, postcode, town, vertical, change_type,
                      event_type, lifecycle_stage, confidence,
                      confidence_breakdown, stage_reason, first_seen_at,
                      latest_update_at FROM opportunities WHERE id = %s""",
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
        "first_seen_at": opportunity[13],
        "latest_update_at": opportunity[14],
        "signals": [dict(zip(fields, row)) for row in rows],
    }


def list_match_reviews(settings: Settings, *, limit: int, offset: int) -> dict[str, Any]:
    with connection(settings) as conn:
        total = conn.execute(
            "SELECT count(*) FROM opportunity_match_reviews WHERE status = 'PENDING'"
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT mr.id, mr.raw_signal_id, mr.opportunity_id, mr.outcome,
                      mr.confidence, mr.reason, mr.created_at,
                      rs.title, rs.source_type, o.name
               FROM opportunity_match_reviews mr
               JOIN raw_signals rs ON rs.id = mr.raw_signal_id
               JOIN opportunities o ON o.id = mr.opportunity_id
               WHERE mr.status = 'PENDING'
               ORDER BY mr.created_at DESC LIMIT %s OFFSET %s""",
            (limit, offset),
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
                  se.event_type, se.lifecycle_stage, se.confidence, se.extracted_facts
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
        creation = opportunity_creation_decision(
            {
                "source_type": source_type,
                "raw_text": raw_text,
                "nursery_name": name,
                "operator_name": organisation,
                "address": location,
                "event_type": event_type,
                "extracted_facts": facts,
                "metadata": metadata,
            }
        )
        opportunity_id = conn.execute(
            """INSERT INTO opportunities
               (name, event_type, lifecycle_stage, confidence, vertical, operator_name,
                address, postcode, town, stage_reason, change_type)
               VALUES (%s, %s, %s, %s, 'NURSERY', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                opportunity_title(
                    {
                        "operator_name": organisation,
                        "address": location,
                        "metadata": metadata,
                        "raw_text": raw_text,
                    },
                    creation,
                ),
                event_type or "other",
                lifecycle or "DISCOVERED",
                confidence or 0,
                organisation,
                location,
                metadata.get("postcode"),
                metadata.get("town") or metadata.get("locality"),
                "created by admin from signal",
                creation.change_type,
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
        if not conn.execute(
            "SELECT 1 FROM opportunities WHERE id = %s", (opportunity_id,)
        ).fetchone():
            raise ValueError("opportunity_not_found")
        signal = _signal_summary(conn, signal_id)
        if signal is None:
            raise ValueError("signal_not_found")
        confidence = signal[-1] or 0
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
        conn.execute(
            "UPDATE opportunities SET latest_update_at = now(), updated_at = now() WHERE id = %s",
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
        if not conn.execute("SELECT 1 FROM opportunities WHERE id = %s", (source_id,)).fetchone():
            raise ValueError("source_opportunity_not_found")
        if not conn.execute("SELECT 1 FROM opportunities WHERE id = %s", (target_id,)).fetchone():
            raise ValueError("target_opportunity_not_found")
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
            "address, postcode, town, change_type FROM opportunities WHERE id = %s",
            (opportunity_id,),
        ).fetchone()
        if not base:
            raise ValueError("opportunity_not_found")
        new_id = conn.execute(
            """INSERT INTO opportunities
               (name, event_type, lifecycle_stage, confidence, vertical, operator_name,
                address, postcode, town, change_type, stage_reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
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
                raw_signal_id, provider, model_id, prompt_version, recommendation,
                confidence, reason, recruitment_relevance, planning_relevance,
                commercial_change_evidence, status, failure_category, attempted_at,
                evaluated_at, input_tokens, output_tokens, latency_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      to_timestamp(%s), to_timestamp(%s), %s, %s, %s)
            ON CONFLICT (raw_signal_id, provider, model_id, prompt_version) DO NOTHING
            RETURNING id""",
            (
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
