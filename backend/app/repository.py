from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.classification import CLASSIFICATION_RULE_VERSION
from app.config import Settings
from app.db import connection
from app.enrichment import fixture_enrichment
from app.ingestion import NormalizedSignal
from app.queueing import EnrichmentMessage, send_enrichment_message


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
                   se.confidence, se.extracted_facts
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
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


def _identity(row: tuple[Any, ...]) -> SignalIdentity:
    return SignalIdentity(
        id=row[0],
        evidence_key=row[1],
        enrichment_queued_at=row[2],
        content_sha256=row[3],
    )
