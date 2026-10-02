from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import boto3
from psycopg.types.json import Jsonb

from app.care_lifecycle import (
    CARE_LIFECYCLE_POLICY_VERSION,
    CARE_PLANNING_WATCHER_POLICY_VERSION,
    CARE_PLANNING_WATCHER_PREVIOUS_MONTHLY_REQUESTS,
    CARE_PUBLICATION_HOLDOUT_VERSION,
    CARE_PUBLICATION_LEGACY_POLICY_VERSION,
    CARE_PUBLICATION_POLICY_VERSION,
    CARE_PUBLICATION_PREVIOUS_POLICY_VERSION,
    CARE_WITHDRAWAL_POLICY_VERSION,
    CareLifecycle,
    LifecycleDecision,
    bootstrap_lifecycle_records,
    classify_publication_conflict,
    derive_care_lifecycle,
    deterministic_initial_poll_at,
    deterministic_policy_samples,
    evaluate_planning_watch,
    evaluate_publication,
    evaluate_publication_v1,
    evaluate_publication_v2,
    evaluate_withdrawal,
    legacy_strong_opening_signals,
    planning_watch_snapshot,
    project_planning_watch_requests,
)
from app.care_planning_review import (
    CARE_PLANNING_AI_APPROVAL_BLOCKED_OUTCOMES,
    CARE_PLANNING_AI_APPROVAL_MIN_CONFIDENCE,
    CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
    CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS,
    CARE_PLANNING_AI_APPROVAL_PROMPT_VERSION,
    CARE_PLANNING_AI_APPROVAL_SUBTYPES,
    CARE_PLANNING_FASTPATH_POLICY_VERSION,
    CARE_PLANNING_LAWFULNESS_MIN_CONFIDENCE,
    CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
    CARE_PLANNING_LAWFULNESS_PROMPT_VERSION,
    CARE_PLANNING_TAXONOMY_VERSION,
    WITHDRAWAL_POLICY_VERSION,
    care_planning_ai_approval_exclusion,
    care_planning_ai_approval_outcome,
    care_planning_ai_approval_qa_bucket,
    care_planning_fastpath_eligible,
    care_planning_fastpath_qa_bucket,
    care_planning_fastpath_qa_holdout,
    care_planning_lawfulness_exclusion,
    care_planning_lawfulness_outcome,
    care_planning_lawfulness_qa_bucket,
    classify_care_planning_subtype,
    planning_withdrawal_assessment,
    validate_care_planning_subtype,
)
from app.classification import CLASSIFICATION_RULE_VERSION
from app.companies_house import (
    OrganisationCandidate,
    compare_company_candidate,
    rank_company_candidates,
)
from app.config import Settings
from app.correlation import (
    classify_match,
    compatible_names,
    normalize_identity,
    preferred_match_reason,
    recruitment_evidence_strength,
)
from app.customer_projection import generated_customer_summary, generated_customer_title
from app.db import connection
from app.evidence_support import (
    EvidenceSupport,
    classify_evidence_support,
    planning_timeline_projection,
)
from app.ingestion import NormalizedSignal
from app.opportunity_hygiene import (
    HYGIENE_CATEGORIES,
    ORPHAN_ROOT_CAUSES,
    audit_opportunities,
    filter_hygiene_items,
    opportunity_admin_touch_types,
)
from app.opportunity_orphan_cleanup import (
    ALREADY_RESOLVED,
    AUTO_RESOLVE,
    summarize_orphan_cleanup,
)
from app.opportunity_orphan_cleanup import (
    POLICY_VERSION as OPPORTUNITY_ORPHAN_CLEANUP_POLICY_VERSION,
)
from app.opportunity_semantic_drift import (
    POLICY_VERSION as OPPORTUNITY_SEMANTIC_DRIFT_POLICY_VERSION,
)
from app.opportunity_semantic_drift import (
    plan_opportunity_semantic_drift,
)
from app.organisation_types import (
    ORGANISATION_TYPES,
    PRIVATE_COMPANY,
    PUBLIC_AUTHORITY,
    UNKNOWN,
    is_public_authority_name,
    looks_like_public_authority,
    public_authority_aliases,
)
from app.planning_families import (
    PLANNING_FAMILY_POLICY_VERSION,
    SUPPORT_ONLY_SUBTYPES,
    PlanningFamilyIdentity,
    followup_can_support_opportunity,
    is_foundational_planning_signal,
    normalize_planning_authority,
    normalize_planning_reference,
    planning_authority,
    primary_planning_reference,
    prior_planning_references,
)
from app.planning_outcomes import (
    PLANNING_OUTCOME_POLICY_VERSION,
    PlanningOutcome,
    canonical_planning_outcome,
    normalize_structured_planning_value,
    structured_planning_fields,
    structured_planning_values,
)
from app.queueing import EnrichmentMessage, send_enrichment_message
from app.recruitment import recruitment_record_from_signal
from app.review_triage import (
    NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION,
    NURSERY_PLANNING_ARBORICULTURE_QA_MODULUS,
    NURSERY_PLANNING_LOSS_POLICY_VERSION,
    NURSERY_PLANNING_LOSS_QA_MODULUS,
    REFUSAL_POLICY_VERSION,
    ROUTINE_RECRUITMENT_POLICY_VERSION,
    SAFE_APPROVAL_MIN_CONFIDENCE,
    SAFE_APPROVAL_POLICY_VERSION,
    SAFE_APPROVAL_VERTICALS,
    deterministic_review_recommendation,
    explicit_nursery_loss_policy_outcome,
    nursery_arboriculture_disagreement_policy_outcome,
    planning_refusal_assessment,
    review_triage_bucket,
    routine_recruitment_policy_outcome,
    routine_recruitment_qa_bucket,
    safe_approval_policy_outcome,
    safe_approval_qa_bucket,
    validate_triage_bucket,
)
from app.verticals import (
    ALL_VERTICALS,
    NURSERY,
    policy_for,
    validate_vertical,
    validate_vertical_filter,
)

PLANNING_FAMILY_HISTORICAL_BACKFILL_VERSION = "planning-family-historical-backfill-v1"


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
    public_authority = is_public_authority_name(display_name)
    company_number = (
        str(metadata.get("companies_house_number") or metadata.get("company_number") or "").strip()
        or None
    )
    if public_authority:
        company_number = None
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
        """SELECT id, name, legal_name, companies_house_number, organisation_type
           FROM operators ORDER BY created_at LIMIT 1000"""
    ).fetchall()
    for row in rows:
        if identity in {normalize_identity(row[1]), normalize_identity(row[2])}:
            if public_authority and len(row) > 4 and row[4] == UNKNOWN and not row[3]:
                conn.execute(
                    """UPDATE operators SET organisation_type = 'PUBLIC_AUTHORITY',
                       organisation_type_source = 'DETERMINISTIC_NAME',
                       organisation_type_updated_at = now(), updated_at = now()
                       WHERE id = %s AND organisation_type = 'UNKNOWN'
                         AND companies_house_number IS NULL""",
                    (row[0],),
                )
                _store_public_authority_aliases(conn, row[0], display_name)
            return row[0]
    alias_match = conn.execute(
        """SELECT operator_id FROM organisation_aliases
           WHERE normalized_alias = %s ORDER BY created_at LIMIT 1""",
        (identity,),
    ).fetchone()
    if alias_match:
        return alias_match[0]
    organisation_type = (
        PUBLIC_AUTHORITY if public_authority else (PRIVATE_COMPANY if company_number else UNKNOWN)
    )
    row = conn.execute(
        """INSERT INTO operators
           (name, legal_name, companies_house_number, website_url, organisation_type,
            organisation_type_source, organisation_type_updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, now()) RETURNING id""",
        (
            display_name,
            display_name,
            company_number,
            website,
            organisation_type,
            "DETERMINISTIC_NAME"
            if public_authority
            else "SOURCE_COMPANY_NUMBER"
            if company_number
            else "UNCLASSIFIED",
        ),
    ).fetchone()
    if public_authority:
        _store_public_authority_aliases(conn, row[0], display_name)
    return row[0]


def _store_public_authority_aliases(conn: Any, operator_id: Any, name: str) -> None:
    for alias in public_authority_aliases(name):
        normalized = normalize_identity(alias)
        if normalized:
            conn.execute(
                """INSERT INTO organisation_aliases
                   (operator_id, alias, normalized_alias, source)
                   VALUES (%s, %s, %s, 'PUBLIC_AUTHORITY_NAME')
                   ON CONFLICT (operator_id, normalized_alias) DO NOTHING""",
                (operator_id, alias, normalized),
            )


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
    database_value = {key: _postgres_text(item) for key, item in value.items()}
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
        provider_name = _validated_organisation_name(database_value.get("registered_provider_name"))
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
    triage_bucket: str | None = None,
    planning_subtype: str | None = None,
) -> dict[str, Any]:
    vertical = validate_vertical_filter(vertical)
    triage_bucket = validate_triage_bucket(triage_bucket)
    planning_subtype = validate_care_planning_subtype(planning_subtype)
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
            "OR COALESCE(se.address, '') ILIKE %s "
            "OR COALESCE(se.extracted_facts->'planning_prior_references', "
            "'[]'::jsonb)::text ILIKE %s "
            "OR COALESCE(rs.planning_reference_normalized, '') ILIKE %s"
            ")"
        )
        params.extend([search_pattern] * 12)
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
    if planning_subtype:
        if planning_subtype == "EXPLICIT_NEW_HOME":
            clauses.append(
                "se.extracted_facts->>'planning_subtype' IN "
                "('NEW_HOME_CHANGE_OF_USE', 'NEW_HOME_OTHER_EXPLICIT')"
            )
        else:
            clauses.append("se.extracted_facts->>'planning_subtype' = %s")
            params.append(planning_subtype)
    if triage_bucket:
        triage_ids = pending_review_triage_ids(settings, vertical=vertical, bucket=triage_bucket)
        if triage_ids:
            clauses.append("rs.id = ANY(%s::uuid[])")
            params.append(triage_ids)
        else:
            clauses.append("FALSE")
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
                   ai.confidence, ai.status, ai.prompt_version
            FROM raw_signals rs
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT recommendation, confidence, status, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC, id DESC
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
        "ai_prompt_version",
    )
    items = [dict(zip(fields, row)) for row in rows]
    for item in items:
        if item["vertical"] != "CHILDRENS_HOME" or item["source_type"] != "planning":
            continue
        if not item.get("ai_prompt_version"):
            item["ai_currency"] = "NO_AI_ASSESSMENT"
        elif item.get("ai_status") == "FAILED":
            item["ai_currency"] = "AI_FAILED"
        elif item["ai_prompt_version"] == settings.ai_care_planning_prompt_version:
            item["ai_currency"] = "CURRENT_V2"
        else:
            item["ai_currency"] = "STALE_V1"
    return {
        "items": items,
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


def apply_safe_approval_policy(
    settings: Settings, signal_id: str, *, trigger_actor: str | None = None
) -> dict[str, Any]:
    """Apply NurserySignal safe-approval-v1 once AI and deterministic evidence exist."""
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, rs.vertical, rs.source_type, rs.metadata, se.review_status,
                   se.extracted_facts, ai.status, ai.recommendation, ai.confidence
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC LIMIT 1
            ) ai ON TRUE
            WHERE rs.id = %s
            FOR UPDATE OF se
            """,
            (signal_id,),
        ).fetchone()
        if row is None:
            return {"signal_id": signal_id, "outcome": "INELIGIBLE", "updated": False}
        fields = (
            "id",
            "vertical",
            "source_type",
            "metadata",
            "review_status",
            "extracted_facts",
            "ai_status",
            "ai_recommendation",
            "ai_confidence",
        )
        item = dict(zip(fields, row))
        confidence = float(item["ai_confidence"]) if item["ai_confidence"] is not None else None
        outcome = safe_approval_policy_outcome(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item["metadata"],
            extracted_facts=item["extracted_facts"],
            ai_status=item["ai_status"],
            ai_recommendation=item["ai_recommendation"],
            ai_confidence=confidence,
        )
        if outcome == "INELIGIBLE":
            return {"signal_id": signal_id, "outcome": outcome, "updated": False}
        qa_bucket = safe_approval_qa_bucket(str(item["id"]))
        marker = {
            "policy_version": SAFE_APPROVAL_POLICY_VERSION,
            "outcome": outcome,
            "threshold": SAFE_APPROVAL_MIN_CONFIDENCE,
            "qa_bucket": qa_bucket,
            "deterministic_recommendation": "APPROVE",
            "ai_recommendation": item["ai_recommendation"],
            "ai_confidence": confidence,
            "source_type": item["source_type"],
            "vertical": item["vertical"],
            "trigger": "admin_backlog" if trigger_actor else "live_enrichment",
        }
        if outcome == "QA_HOLDOUT":
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                  AND COALESCE(extracted_facts->'safe_approval'->>'policy_version', '') <> %s
                RETURNING raw_signal_id
                """,
                (Jsonb({"safe_approval": marker}), signal_id, SAFE_APPROVAL_POLICY_VERSION),
            ).fetchone()
            action = "SAFE_AGREEMENT_QA_HOLDOUT"
        else:
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET review_status = 'APPROVED', reviewed_by = %s, reviewed_at = now(),
                    extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                RETURNING raw_signal_id
                """,
                (
                    f"system:{SAFE_APPROVAL_POLICY_VERSION}",
                    Jsonb({"safe_approval": marker}),
                    signal_id,
                ),
            ).fetchone()
            action = "SAFE_AGREEMENT_AUTO_APPROVE"
        if updated:
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (action, actor, target_type, target_count, details, vertical)
                VALUES (%s, %s, 'signal', 1, %s, %s)
                """,
                (
                    action,
                    trigger_actor or f"system:{SAFE_APPROVAL_POLICY_VERSION}",
                    Jsonb({"signal_id": signal_id, **marker}),
                    item["vertical"],
                ),
            )
            conn.commit()
        return {"signal_id": signal_id, "outcome": outcome, "updated": bool(updated)}


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


def _auto_reject_refused_planning_with_connection(
    conn: Any,
    signal_id: str,
    metadata: dict[str, Any] | None,
    *,
    actor: str,
) -> bool:
    outcome = canonical_planning_outcome(metadata)
    if not outcome.refused:
        return False
    reason = "Automatically rejected — planning permission refused by council."
    review_facts = {
        "automatic_review": {
            "status": "REJECTED",
            "reason": reason,
            "policy_version": REFUSAL_POLICY_VERSION,
            "outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
            "planning_outcome": outcome.outcome.value,
            "planning_decision": outcome.matched_value,
            "planning_outcome_field": outcome.matched_field,
            "decision_date": outcome.decision_date,
        }
    }
    row = conn.execute(
        """
        UPDATE signal_enrichments se
        SET review_status = 'REJECTED', reviewed_by = %s, reviewed_at = now(),
            extracted_facts = se.extracted_facts || %s, updated_at = now()
        FROM raw_signals rs
        WHERE se.raw_signal_id = rs.id AND rs.id = %s
          AND rs.source_type = 'planning' AND se.review_status = 'PENDING'
        RETURNING rs.vertical
        """,
        (actor, Jsonb(review_facts), signal_id),
    ).fetchone()
    if row:
        conn.execute(
            """
            INSERT INTO admin_audit_events
                (action, actor, target_type, target_count, details, vertical)
            VALUES ('PLANNING_REFUSAL_AUTO_REJECT', %s, 'signal', 1, %s, %s)
            """,
            (
                actor,
                Jsonb(
                    {
                        "signal_id": signal_id,
                        "reason": reason,
                        "policy_version": REFUSAL_POLICY_VERSION,
                        "outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
                        "planning_outcome": outcome.outcome.value,
                        "planning_decision": outcome.matched_value,
                        "planning_outcome_field": outcome.matched_field,
                        "decision_date": outcome.decision_date,
                    }
                ),
                row[0],
            ),
        )
    return row is not None


def auto_reject_refused_planning(
    settings: Settings,
    signal_id: str,
    metadata: dict[str, Any] | None,
    *,
    actor: str = f"system:{REFUSAL_POLICY_VERSION}",
) -> bool:
    """Reject one pending signal from an explicit structured council refusal only."""
    with connection(settings) as conn:
        updated = _auto_reject_refused_planning_with_connection(
            conn, signal_id, metadata, actor=actor
        )
        conn.commit()
    return updated


def _auto_reject_withdrawn_planning_with_connection(
    conn: Any,
    signal_id: str,
    metadata: dict[str, Any] | None,
    *,
    actor: str,
) -> bool:
    outcome = canonical_planning_outcome(metadata)
    if not outcome.withdrawn:
        return False
    reason = "Automatically rejected — planning application withdrawn."
    marker = {
        "status": "REJECTED",
        "reason": reason,
        "policy_version": WITHDRAWAL_POLICY_VERSION,
        "outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
        "planning_outcome": outcome.outcome.value,
        "planning_decision": outcome.matched_value,
        "planning_outcome_field": outcome.matched_field,
        "decision_date": outcome.decision_date,
    }
    row = conn.execute(
        """
        UPDATE signal_enrichments se
        SET review_status = 'REJECTED', reviewed_by = %s, reviewed_at = now(),
            extracted_facts = se.extracted_facts || %s, updated_at = now()
        FROM raw_signals rs
        WHERE se.raw_signal_id = rs.id AND rs.id = %s
          AND rs.source_type = 'planning' AND se.review_status = 'PENDING'
        RETURNING rs.vertical
        """,
        (actor, Jsonb({"automatic_review": marker}), signal_id),
    ).fetchone()
    if row:
        conn.execute(
            """
            INSERT INTO admin_audit_events
                (action, actor, target_type, target_count, details, vertical)
            VALUES ('PLANNING_WITHDRAWAL_AUTO_REJECT', %s, 'signal', 1, %s, %s)
            """,
            (
                actor,
                Jsonb({"signal_id": signal_id, **marker}),
                row[0],
            ),
        )
    return row is not None


def auto_reject_withdrawn_planning(
    settings: Settings,
    signal_id: str,
    metadata: dict[str, Any] | None,
    *,
    actor: str = f"system:{WITHDRAWAL_POLICY_VERSION}",
) -> bool:
    """Reject one pending signal from an exact structured withdrawal only."""
    with connection(settings) as conn:
        updated = _auto_reject_withdrawn_planning_with_connection(
            conn, signal_id, metadata, actor=actor
        )
        conn.commit()
    return updated


def planning_outcome_dry_run(settings: Settings, *, limit: int = 2500) -> dict[str, Any]:
    """Read-only report of canonical outcomes for pending CareProspect Planning.

    The report also inventories the real structured provider vocabulary across
    the bounded CareProspect Planning corpus. It never changes reviews,
    opportunities, relationships, AI assessments, or publication state.
    """
    bounded_limit = min(max(int(limit), 1), 2500)
    with connection(settings) as conn:
        pending_rows = conn.execute(
            """
            SELECT rs.id, rs.external_id, rs.title, rs.metadata,
                   se.extracted_facts,
                   EXISTS (
                       SELECT 1
                       FROM opportunity_signals os
                       JOIN opportunities o ON o.id = os.opportunity_id
                       WHERE os.raw_signal_id = rs.id
                         AND os.status = 'ACTIVE'
                         AND o.publication_status = 'PUBLISHED'
                   ) AS affects_published
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME'
              AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            (bounded_limit,),
        ).fetchall()
        vocabulary_rows = conn.execute(
            """
            SELECT rs.metadata
            FROM raw_signals rs
            WHERE rs.vertical = 'CHILDRENS_HOME'
              AND rs.source_type = 'planning'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            (bounded_limit,),
        ).fetchall()

    outcome_counts: Counter[str] = Counter()
    override_counts: Counter[str] = Counter()
    representatives: dict[str, list[dict[str, Any]]] = {}
    published_ids: set[str] = set()
    for (
        signal_id,
        external_id,
        title,
        raw_metadata,
        extracted_facts,
        affects_published,
    ) in pending_rows:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        facts = extracted_facts if isinstance(extracted_facts, dict) else {}
        assessment = canonical_planning_outcome(metadata)
        outcome = assessment.outcome.value
        outcome_counts[outcome] += 1
        if facts.get("opportunity_creation_decision") == "CREATE_OPPORTUNITY" and (
            assessment.terminal_negative
            or assessment.outcome is PlanningOutcome.REFUSED_UNDER_APPEAL
        ):
            override_counts[outcome] += 1
        if affects_published:
            published_ids.add(str(signal_id))
        bucket = representatives.setdefault(outcome, [])
        if len(bucket) < 5:
            bucket.append(
                {
                    "signal_id": str(signal_id),
                    "provider_reference": str(external_id or ""),
                    "title": str(title or "")[:240],
                    "council": metadata.get("council"),
                    "planning_status": metadata.get("planning_status"),
                    "decision": metadata.get("decision"),
                    "decision_date": assessment.decision_date,
                    "matched_field": assessment.matched_field,
                    "matched_value": assessment.matched_value,
                    "current_opportunity_action": facts.get("opportunity_creation_decision"),
                    "affects_published": bool(affects_published),
                }
            )

    concepts = re.compile(
        r"\b(?:REFUS\w*|REJECT\w*|WITHDRAW\w*|APPROV\w*|GRANT\w*|PERMISSION|"
        r"APPEAL\w*|DISMISS\w*|ALLOW\w*)\b"
    )
    vocabulary: Counter[tuple[str, str]] = Counter()
    for (raw_metadata,) in vocabulary_rows:
        for field, raw_value in structured_planning_fields(raw_metadata):
            value = str(raw_value or "").strip()
            normalized = normalize_structured_planning_value(value)
            if normalized and concepts.search(normalized):
                vocabulary[(field, value)] += 1

    return {
        "policy_version": PLANNING_OUTCOME_POLICY_VERSION,
        "read_only": True,
        "pending_inspected": len(pending_rows),
        "inspection_limit": bounded_limit,
        "outcomes": dict(sorted(outcome_counts.items())),
        "current_create_opportunity_overrides": dict(sorted(override_counts.items())),
        "terminal_negative_total": sum(
            outcome_counts.get(outcome.value, 0)
            for outcome in (
                PlanningOutcome.REFUSED,
                PlanningOutcome.WITHDRAWN,
                PlanningOutcome.APPEAL_DISMISSED,
            )
        ),
        "refused_under_active_appeal": outcome_counts.get(
            PlanningOutcome.REFUSED_UNDER_APPEAL.value, 0
        ),
        "published_opportunity_signal_count": len(published_ids),
        "representatives": representatives,
        "provider_vocabulary": [
            {"field": field, "value": value, "count": count}
            for (field, value), count in sorted(
                vocabulary.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
            )[:150]
        ],
    }


def cleanup_withdrawn_care_planning_signals(
    settings: Settings, *, actor: str, limit: int = 2000
) -> dict[str, Any]:
    """Boundedly remove exact CareProspect withdrawals from the pending inbox."""
    bounded_limit = min(max(int(limit), 1), 2500)
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.id, rs.metadata
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            (bounded_limit,),
        ).fetchall()
        withdrawn = [
            (str(row[0]), row[1] or {})
            for row in rows
            if planning_withdrawal_assessment(row[1]).withdrawn
        ]
        updated = 0
        errors = 0
        for signal_id, metadata in withdrawn:
            try:
                with conn.transaction():
                    updated += int(
                        _auto_reject_withdrawn_planning_with_connection(
                            conn,
                            signal_id,
                            metadata,
                            actor=f"system:{WITHDRAWAL_POLICY_VERSION}",
                        )
                    )
            except Exception:
                errors += 1
        conn.execute(
            """
            INSERT INTO admin_audit_events
                (action, actor, target_type, target_count, details, vertical)
            VALUES ('PLANNING_WITHDRAWAL_CLEANUP', %s, 'signal', %s, %s, 'CHILDRENS_HOME')
            """,
            (
                actor,
                updated,
                Jsonb(
                    {
                        "policy_version": WITHDRAWAL_POLICY_VERSION,
                        "inspected": len(rows),
                        "withdrawals_found": len(withdrawn),
                        "auto_rejected": updated,
                        "errors": errors,
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "policy_version": WITHDRAWAL_POLICY_VERSION,
        "pending_care_planning_inspected": len(rows),
        "inspection_limit": bounded_limit,
        "withdrawals_found": len(withdrawn),
        "auto_rejected": updated,
        "errors": errors,
    }


_CARE_PLANNING_CLASSIFICATION_KEYS = (
    "planning_subtype",
    "planning_subtype_reasons",
    "explicit_new_home_proposal",
    "planning_material_capacity_change",
    "planning_prior_references",
    "planning_ambiguity_markers",
    "opportunity_creation_decision",
    "opportunity_change_type",
)

_CARE_PLANNING_PRESERVED_MARKERS = (
    "automatic_review",
    "safe_approval",
    "care_planning_fastpath",
    "care_planning_ai_approval",
    "care_planning_lawfulness_approval",
)


def _pending_care_planning_taxonomy_rows(
    settings: Settings, limit: int = 2500
) -> list[dict[str, Any]]:
    bounded_limit = min(max(int(limit), 1), 2500)
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url,
                   rs.external_id, rs.discovered_at, rs.title, rs.raw_text,
                   rs.location_hint, rs.organisation_hint, rs.metadata, rs.vertical,
                   se.review_status, se.reviewed_by, se.extracted_facts,
                   ai.status, ai.recommendation, ai.confidence, ai.prompt_version
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) ai ON TRUE
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            (bounded_limit,),
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
        "review_status",
        "reviewed_by",
        "existing_facts",
        "ai_status",
        "ai_recommendation",
        "ai_confidence",
        "ai_prompt_version",
    )
    return [dict(zip(fields, row)) for row in rows]


def _care_planning_taxonomy_evaluations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    for item in items:
        candidate = policy_for("CHILDRENS_HOME").classify_signal(item)
        facts = dict(candidate.get("extracted_facts") or {})
        existing = item.get("existing_facts") or {}
        for marker_name in _CARE_PLANNING_PRESERVED_MARKERS:
            if marker_name in existing:
                facts[marker_name] = existing[marker_name]
        old_subtype = str(existing.get("planning_subtype") or "AMBIGUOUS")
        new_subtype = str(facts.get("planning_subtype") or "AMBIGUOUS")
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        before_bucket = review_triage_bucket(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item.get("metadata"),
            extracted_facts=existing,
            ai_status=item.get("ai_status"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        after_bucket = review_triage_bucket(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item.get("metadata"),
            extracted_facts=facts,
            ai_status=item.get("ai_status"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        evaluations.append(
            {
                "item": item,
                "candidate": candidate,
                "facts": facts,
                "old_subtype": old_subtype,
                "new_subtype": new_subtype,
                "changed": any(
                    existing.get(key) != facts.get(key)
                    for key in _CARE_PLANNING_CLASSIFICATION_KEYS
                ),
                "before_bucket": before_bucket,
                "after_bucket": after_bucket,
            }
        )
    return evaluations


def _increment(counter: dict[str, int], value: Any) -> None:
    key = str(value if value not in (None, "") else "MISSING")
    counter[key] = counter.get(key, 0) + 1


def care_planning_taxonomy_preview(settings: Settings, *, limit: int = 2500) -> dict[str, Any]:
    """Recompute the pending taxonomy in memory without changing review or evidence state."""
    items = _pending_care_planning_taxonomy_rows(settings, limit)
    evaluations = _care_planning_taxonomy_evaluations(items)
    before_subtypes: dict[str, int] = {}
    after_subtypes: dict[str, int] = {}
    before_triage: dict[str, int] = {}
    after_triage: dict[str, int] = {}
    transitions: dict[str, dict[str, Any]] = {}
    for evaluation in evaluations:
        item = evaluation["item"]
        facts = evaluation["facts"]
        _increment(before_subtypes, evaluation["old_subtype"])
        _increment(after_subtypes, evaluation["new_subtype"])
        _increment(before_triage, evaluation["before_bucket"])
        _increment(after_triage, evaluation["after_bucket"])
        transition_key = f"{evaluation['old_subtype']} -> {evaluation['new_subtype']}"
        transition = transitions.setdefault(
            transition_key,
            {
                "count": 0,
                "ai_recommendations": {},
                "confidence_distribution": {},
                "canonical_outcomes": {},
                "opportunity_creation_decisions": {},
                "examples": [],
            },
        )
        transition["count"] += 1
        _increment(transition["ai_recommendations"], item.get("ai_recommendation"))
        _increment(
            transition["confidence_distribution"],
            f"{float(item['ai_confidence']):.2f}"
            if item.get("ai_confidence") is not None
            else None,
        )
        _increment(
            transition["canonical_outcomes"],
            canonical_planning_outcome(item.get("metadata")).outcome.value,
        )
        _increment(
            transition["opportunity_creation_decisions"],
            facts.get("opportunity_creation_decision"),
        )
        if evaluation["changed"] and len(transition["examples"]) < 3:
            transition["examples"].append(
                {
                    "external_id": item.get("external_id"),
                    "title": str(item.get("title") or "")[:500],
                    "ai_recommendation": item.get("ai_recommendation"),
                    "ai_confidence": item.get("ai_confidence"),
                }
            )

    explicit_eligible: list[str] = []
    explicit_holdouts = 0
    lawfulness_eligible: list[str] = []
    lawfulness_holdouts = 0
    for evaluation in evaluations:
        item = evaluation["item"]
        facts = evaluation["facts"]
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        policy_kwargs = {
            "signal_id": str(item["id"]),
            "vertical": item["vertical"],
            "source_type": item["source_type"],
            "review_status": item["review_status"],
            "reviewed_by": item.get("reviewed_by"),
            "metadata": item.get("metadata"),
            "extracted_facts": facts,
            "ai_status": item.get("ai_status"),
            "ai_prompt_version": item.get("ai_prompt_version"),
            "ai_recommendation": item.get("ai_recommendation"),
            "ai_confidence": confidence,
        }
        explicit_outcome = care_planning_ai_approval_outcome(**policy_kwargs)
        if explicit_outcome != "NOT_ELIGIBLE":
            explicit_eligible.append(evaluation["new_subtype"])
            explicit_holdouts += int(explicit_outcome == "QA_HOLDOUT")
        lawfulness_outcome = care_planning_lawfulness_outcome(**policy_kwargs)
        if lawfulness_outcome != "NOT_ELIGIBLE":
            lawfulness_eligible.append(evaluation["new_subtype"])
            lawfulness_holdouts += int(lawfulness_outcome == "QA_HOLDOUT")

    changed_ids = [evaluation["item"]["id"] for evaluation in evaluations if evaluation["changed"]]
    published_affected = 0
    if changed_ids:
        with connection(settings) as conn:
            published_affected = int(
                conn.execute(
                    """
                    SELECT count(DISTINCT o.id)
                    FROM opportunities o
                    JOIN opportunity_signals os ON os.opportunity_id = o.id
                    WHERE o.vertical = 'CHILDRENS_HOME'
                      AND o.publication_status = 'PUBLISHED'
                      AND os.raw_signal_id = ANY(%s::uuid[])
                    """,
                    (changed_ids,),
                ).fetchone()[0]
            )
    return {
        "preview": True,
        "policy_version": CARE_PLANNING_TAXONOMY_VERSION,
        "pending_evaluated": len(evaluations),
        "records_changed": len(changed_ids),
        "before_subtypes": dict(sorted(before_subtypes.items())),
        "after_subtypes": dict(sorted(after_subtypes.items())),
        "before_triage": dict(sorted(before_triage.items())),
        "after_triage": dict(sorted(after_triage.items())),
        "transitions": dict(sorted(transitions.items())),
        "policy_preview": {
            "care_planning_ai_approval_v1_1": {
                "eligible": len(explicit_eligible),
                "would_auto_approve": len(explicit_eligible) - explicit_holdouts,
                "qa_holdouts": explicit_holdouts,
                "subtypes": dict(sorted(Counter(explicit_eligible).items())),
            },
            "care_planning_lawfulness_proposed_v1": {
                "eligible": len(lawfulness_eligible),
                "would_auto_approve": len(lawfulness_eligible) - lawfulness_holdouts,
                "qa_holdouts": lawfulness_holdouts,
                "subtypes": dict(sorted(Counter(lawfulness_eligible).items())),
            },
        },
        "published_opportunities_affected": published_affected,
        "customer_publication_unchanged": True,
        "review_state_unchanged": True,
        "ai_history_unchanged": True,
    }


def reclassify_pending_care_planning(
    settings: Settings,
    *,
    actor: str,
    limit: int = 2000,
    signal_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Recompute pending CareProspect Planning subtype facts without touching labels."""
    bounded_limit = min(max(int(limit), 1), 2500)
    targeted_ids = list(dict.fromkeys(signal_ids or []))
    if len(targeted_ids) > 100:
        raise ValueError("signal_ids_exceed_limit")
    targeted_clause = " AND rs.id = ANY(%s::uuid[])" if targeted_ids else ""
    params: list[Any] = []
    if targeted_ids:
        params.append(targeted_ids)
        bounded_limit = min(bounded_limit, len(targeted_ids))
    params.append(bounded_limit)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT rs.id, rs.schema_version, rs.source_type, rs.source_url,
                   rs.external_id, rs.discovered_at, rs.title, rs.raw_text,
                   rs.location_hint, rs.organisation_hint, rs.metadata, rs.vertical,
                   se.review_status, se.extracted_facts
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
              {targeted_clause}
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
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
            "review_status",
            "existing_facts",
        )
        counts: dict[str, int] = {}
        transitions: dict[str, int] = {}
        updated = 0
        records_changed = 0
        idempotent_skips = 0
        errors = 0
        for row in rows:
            raw = dict(zip(fields, row))
            try:
                candidate = policy_for("CHILDRENS_HOME").classify_signal(raw)
                facts = candidate["extracted_facts"]
                existing = raw.get("existing_facts") or {}
                for marker_name in _CARE_PLANNING_PRESERVED_MARKERS:
                    if marker_name in existing:
                        facts[marker_name] = existing[marker_name]
                old_subtype = str(existing.get("planning_subtype") or "AMBIGUOUS")
                subtype = str(facts.get("planning_subtype") or "AMBIGUOUS")
                counts[subtype] = counts.get(subtype, 0) + 1
                transition = f"{old_subtype} -> {subtype}"
                transitions[transition] = transitions.get(transition, 0) + 1
                semantic_change = any(
                    existing.get(key) != facts.get(key)
                    for key in _CARE_PLANNING_CLASSIFICATION_KEYS
                )
                if semantic_change:
                    records_changed += 1
                    history = list(existing.get("planning_classification_history") or [])
                    history.append(
                        {
                            "taxonomy_version": existing.get("planning_taxonomy_version")
                            or "legacy",
                            "planning_subtype": existing.get("planning_subtype"),
                            "planning_subtype_reasons": existing.get("planning_subtype_reasons")
                            or [],
                            "opportunity_creation_decision": existing.get(
                                "opportunity_creation_decision"
                            ),
                            "opportunity_change_type": existing.get("opportunity_change_type"),
                            "reclassified_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    facts["planning_classification_history"] = history[-10:]
                elif existing.get("planning_classification_history"):
                    facts["planning_classification_history"] = existing[
                        "planning_classification_history"
                    ]
                if (
                    not semantic_change
                    and existing.get("planning_taxonomy_version") == CARE_PLANNING_TAXONOMY_VERSION
                ):
                    idempotent_skips += 1
                    continue
                changed = conn.execute(
                    """
                    UPDATE signal_enrichments
                    SET schema_version = %s, event_type = %s, nursery_name = %s,
                        operator_name = %s, address = %s, expected_opening_date = %s,
                        capacity = %s, lifecycle_stage = %s, confidence = %s,
                        extracted_facts = %s, evidence = %s, updated_at = now()
                    WHERE raw_signal_id = %s AND review_status = 'PENDING'
                    RETURNING raw_signal_id
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
                        Jsonb(facts),
                        Jsonb(candidate["evidence"]),
                        raw["id"],
                    ),
                ).fetchone()
                updated += int(changed is not None)
            except Exception:
                errors += 1
        if updated:
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (action, actor, target_type, target_count, details, vertical)
                VALUES ('CARE_PLANNING_SUBTYPE_RECLASSIFY', %s, 'signal', %s, %s,
                        'CHILDRENS_HOME')
                """,
                (
                    actor,
                    updated,
                    Jsonb(
                        {
                            "policy_version": CARE_PLANNING_TAXONOMY_VERSION,
                            "inspected": len(rows),
                            "updated": updated,
                            "records_changed": records_changed,
                            "idempotent_skips": idempotent_skips,
                            "subtypes": counts,
                            "transitions": transitions,
                            "errors": errors,
                            "targeted": bool(targeted_ids),
                            "requested_signal_ids": targeted_ids,
                        }
                    ),
                ),
            )
        conn.commit()
    return {
        "policy_version": CARE_PLANNING_TAXONOMY_VERSION,
        "pending_inspected": len(rows),
        "updated": updated,
        "records_changed": records_changed,
        "idempotent_skips": idempotent_skips,
        "subtypes": counts,
        "transitions": transitions,
        "errors": errors,
        "targeted": bool(targeted_ids),
        "requested_signal_ids": targeted_ids,
        "manually_reviewed_untouched": True,
    }


def care_planning_fastpath_backlog(
    settings: Settings, *, actor: str, preview: bool, limit: int = 100
) -> dict[str, Any]:
    """Preview/apply one bounded batch of the exact Care Planning fast path."""
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.id, rs.vertical, rs.source_type, rs.metadata, se.review_status,
                   se.extracted_facts
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT 2500
            """
        ).fetchall()
        items = [
            dict(
                zip(
                    (
                        "id",
                        "vertical",
                        "source_type",
                        "metadata",
                        "review_status",
                        "extracted_facts",
                    ),
                    row,
                )
            )
            for row in rows
        ]
        eligible = [
            item
            for item in items
            if not planning_refusal_assessment(item["metadata"]).refused
            and not planning_withdrawal_assessment(item["metadata"]).withdrawn
            and care_planning_fastpath_eligible(
                vertical=item["vertical"],
                source_type=item["source_type"],
                review_status=item["review_status"],
                extracted_facts=item["extracted_facts"],
            )
        ]
        subtype_counts: dict[str, int] = {}
        for item in items:
            subtype = str((item["extracted_facts"] or {}).get("planning_subtype") or "AMBIGUOUS")
            subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
        support_only = sum(
            (item["extracted_facts"] or {}).get("opportunity_creation_decision")
            == "SUPPORT_EXISTING_ONLY"
            for item in items
        )
        manual = len(items) - len(eligible)
        unsupported = conn.execute(
            """
            SELECT count(DISTINCT o.id)
            FROM opportunities o
            WHERE o.vertical = 'CHILDRENS_HOME' AND o.publication_status = 'DRAFT'
              AND EXISTS (
                SELECT 1 FROM opportunity_signals os
                JOIN signal_enrichments se ON se.raw_signal_id = os.raw_signal_id
                WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                  AND se.extracted_facts->>'planning_subtype' IN
                    ('REFUSED', 'WITHDRAWN', 'LAWFULNESS_EXISTING',
                     'CONDITION_DISCHARGE', 'NON_MATERIAL_AMENDMENT', 'FOLLOW_UP_OTHER')
              )
              AND NOT EXISTS (
                SELECT 1 FROM opportunity_signals os
                JOIN signal_enrichments se ON se.raw_signal_id = os.raw_signal_id
                WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                  AND COALESCE(se.extracted_facts->>'planning_subtype', '') NOT IN
                    ('REFUSED', 'WITHDRAWN', 'LAWFULNESS_EXISTING',
                     'CONDITION_DISCHARGE', 'NON_MATERIAL_AMENDMENT', 'FOLLOW_UP_OTHER')
                  AND se.review_status <> 'REJECTED'
              )
            """
        ).fetchone()[0]
    auto_ids = [
        str(item["id"])
        for item in eligible
        if not care_planning_fastpath_qa_holdout(str(item["id"]))
    ]
    holdout_ids = [
        str(item["id"]) for item in eligible if care_planning_fastpath_qa_holdout(str(item["id"]))
    ]
    batch_ids = [str(item["id"]) for item in eligible[:bounded_limit]]
    if preview:
        return {
            "preview": True,
            "policy_version": CARE_PLANNING_FASTPATH_POLICY_VERSION,
            "pending_evaluated": len(items),
            "subtypes": subtype_counts,
            "eligible_total": len(eligible),
            "would_auto_approve": len(auto_ids),
            "qa_holdouts": len(holdout_ids),
            "support_only": support_only,
            "manual_remainder": manual,
            "unsupported_draft_opportunities": int(unsupported),
            "batch_limit": bounded_limit,
            "customer_publication_unchanged": True,
        }
    results = [
        apply_care_planning_fastpath_policy(settings, signal_id, trigger_actor=actor)
        for signal_id in batch_ids
    ]
    return {
        "preview": False,
        "policy_version": CARE_PLANNING_FASTPATH_POLICY_VERSION,
        "requested": len(batch_ids),
        "updated": sum(item["updated"] for item in results),
        "auto_approved": sum(
            item["updated"] and item["outcome"] == "AUTO_APPROVE" for item in results
        ),
        "qa_holdouts": sum(item["updated"] and item["outcome"] == "QA_HOLDOUT" for item in results),
        "customer_publication_unchanged": True,
    }


def apply_care_planning_fastpath_policy(
    settings: Settings, signal_id: str, *, trigger_actor: str | None = None
) -> dict[str, Any]:
    """Apply deterministic CareProspect explicit-new-home review policy once."""
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, rs.vertical, rs.source_type, rs.metadata, se.review_status,
                   se.extracted_facts
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.id = %s
            FOR UPDATE OF se
            """,
            (signal_id,),
        ).fetchone()
        if row is None:
            return {"signal_id": signal_id, "outcome": "INELIGIBLE", "updated": False}
        fields = (
            "id",
            "vertical",
            "source_type",
            "metadata",
            "review_status",
            "extracted_facts",
        )
        item = dict(zip(fields, row))
        if (
            planning_refusal_assessment(item["metadata"]).refused
            or planning_withdrawal_assessment(item["metadata"]).withdrawn
        ):
            return {"signal_id": signal_id, "outcome": "INELIGIBLE", "updated": False}
        if not care_planning_fastpath_eligible(
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            extracted_facts=item["extracted_facts"],
        ):
            return {"signal_id": signal_id, "outcome": "INELIGIBLE", "updated": False}
        outcome = (
            "QA_HOLDOUT" if care_planning_fastpath_qa_holdout(str(item["id"])) else "AUTO_APPROVE"
        )
        qa_bucket = care_planning_fastpath_qa_bucket(str(item["id"]))
        facts = item["extracted_facts"] or {}
        marker = {
            "policy_version": CARE_PLANNING_FASTPATH_POLICY_VERSION,
            "outcome": outcome,
            "qa_bucket": qa_bucket,
            "planning_subtype": facts.get("planning_subtype"),
            "planning_decision": next(
                (
                    normalize_structured_planning_value(value)
                    for value in structured_planning_values(item["metadata"])
                    if normalize_structured_planning_value(value)
                ),
                None,
            ),
            "deterministic_evidence": facts.get("planning_subtype_reasons") or [],
            "source_type": item["source_type"],
            "vertical": item["vertical"],
            "trigger": "admin_backlog" if trigger_actor else "live_enrichment",
        }
        if outcome == "QA_HOLDOUT":
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                  AND COALESCE(
                        extracted_facts->'care_planning_fastpath'->>'policy_version', ''
                      ) <> %s
                RETURNING raw_signal_id
                """,
                (
                    Jsonb({"care_planning_fastpath": marker}),
                    signal_id,
                    CARE_PLANNING_FASTPATH_POLICY_VERSION,
                ),
            ).fetchone()
            action = "CARE_PLANNING_FASTPATH_QA_HOLDOUT"
        else:
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET review_status = 'APPROVED', reviewed_by = %s, reviewed_at = now(),
                    extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                RETURNING raw_signal_id
                """,
                (
                    f"system:{CARE_PLANNING_FASTPATH_POLICY_VERSION}",
                    Jsonb({"care_planning_fastpath": marker}),
                    signal_id,
                ),
            ).fetchone()
            action = "CARE_PLANNING_FASTPATH_AUTO_APPROVE"
        if updated:
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (action, actor, target_type, target_count, details, vertical)
                VALUES (%s, %s, 'signal', 1, %s, 'CHILDRENS_HOME')
                """,
                (
                    action,
                    trigger_actor or f"system:{CARE_PLANNING_FASTPATH_POLICY_VERSION}",
                    Jsonb(
                        {
                            "signal_id": signal_id,
                            "reason": (
                                "Auto-approved — explicit new children’s-home planning proposal."
                            ),
                            **marker,
                        }
                    ),
                ),
            )
            conn.commit()
        return {"signal_id": signal_id, "outcome": outcome, "updated": bool(updated)}


def _care_planning_ai_approval_rows(settings: Settings) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT rs.id, rs.external_id, rs.vertical, rs.source_type, rs.metadata,
                   se.review_status,
                   se.reviewed_by, se.extracted_facts, ai.status, ai.recommendation,
                   ai.confidence, ai.prompt_version
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) ai ON TRUE
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status = 'PENDING'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT 5000
            """
        ).fetchall()
    fields = (
        "id",
        "external_id",
        "vertical",
        "source_type",
        "metadata",
        "review_status",
        "reviewed_by",
        "extracted_facts",
        "ai_status",
        "ai_recommendation",
        "ai_confidence",
        "ai_prompt_version",
    )
    return [dict(zip(fields, row)) for row in rows]


def _taxonomy_v2_reclassified_candidate(item: dict[str, Any]) -> bool:
    """Limit a historical catch-up to records whose semantics changed under taxonomy v2."""
    facts = item.get("extracted_facts") or {}
    if facts.get("planning_taxonomy_version") != CARE_PLANNING_TAXONOMY_VERSION:
        return False
    history = facts.get("planning_classification_history") or []
    if not isinstance(history, list) or not history:
        return False
    prior = history[-1] if isinstance(history[-1], dict) else {}
    return prior.get("planning_subtype") != facts.get("planning_subtype")


def _care_planning_ai_approval_evaluation(
    items: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], str]], dict[str, int], dict[str, int]]:
    eligible: list[tuple[dict[str, Any], str]] = []
    exclusions: dict[str, int] = {}
    excluded_subtypes: dict[str, int] = {}
    for item in items:
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        reason = care_planning_ai_approval_exclusion(
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            reviewed_by=item.get("reviewed_by"),
            metadata=item.get("metadata"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_prompt_version=item.get("ai_prompt_version"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            if reason == "SUBTYPE":
                subtype = str(
                    (item.get("extracted_facts") or {}).get("planning_subtype") or "AMBIGUOUS"
                )
                excluded_subtypes[subtype] = excluded_subtypes.get(subtype, 0) + 1
            continue
        outcome = care_planning_ai_approval_outcome(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            reviewed_by=item.get("reviewed_by"),
            metadata=item.get("metadata"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_prompt_version=item.get("ai_prompt_version"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        eligible.append((item, outcome))
    return eligible, exclusions, excluded_subtypes


def care_planning_ai_approval_preview(
    settings: Settings, *, limit: int = 100, taxonomy_catchup_only: bool = False
) -> dict[str, Any]:
    """Preview the exact current CareProspect AI approval cohort without mutation."""
    bounded_limit = min(max(int(limit), 1), 100)
    items = _care_planning_ai_approval_rows(settings)
    eligible, exclusions, excluded_subtypes = _care_planning_ai_approval_evaluation(items)
    if taxonomy_catchup_only:
        eligible = [item for item in eligible if _taxonomy_v2_reclassified_candidate(item[0])]
    subtype_counts = {subtype: 0 for subtype in sorted(CARE_PLANNING_AI_APPROVAL_SUBTYPES)}
    for item, _ in eligible:
        subtype = str((item.get("extracted_facts") or {}).get("planning_subtype"))
        subtype_counts[subtype] += 1
    return {
        "preview": True,
        "policy_version": CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
        "prompt_version": CARE_PLANNING_AI_APPROVAL_PROMPT_VERSION,
        "confidence_threshold": CARE_PLANNING_AI_APPROVAL_MIN_CONFIDENCE,
        "total_pending": len(items),
        "eligible_before_holdout": len(eligible),
        "would_auto_approve": sum(outcome == "AUTO_APPROVE" for _, outcome in eligible),
        "qa_holdouts": sum(outcome == "QA_HOLDOUT" for _, outcome in eligible),
        "eligible_subtypes": subtype_counts,
        "excluded": {
            "by_subtype": exclusions.get("SUBTYPE", 0),
            "by_subtype_distribution": excluded_subtypes,
            "by_ai_recommendation": exclusions.get("AI_RECOMMENDATION", 0),
            "by_confidence": exclusions.get("AI_CONFIDENCE", 0),
            "by_ai_version_or_status": exclusions.get("AI_VERSION_OR_STATUS", 0),
            "by_planning_outcome": exclusions.get("PLANNING_OUTCOME", 0),
            "by_ambiguity_or_false_positive": exclusions.get("AMBIGUITY_OR_FALSE_POSITIVE", 0),
            "by_existing_review_or_policy_state": exclusions.get(
                "EXISTING_REVIEW_OR_POLICY_STATE", 0
            ),
            "out_of_scope": exclusions.get("OUT_OF_SCOPE", 0),
        },
        "batch_limit": bounded_limit,
        "batch_count": min(len(eligible), bounded_limit),
        "taxonomy_catchup_only": taxonomy_catchup_only,
        "customer_publication_unchanged": True,
    }


def apply_care_planning_ai_approval_policy(
    settings: Settings, signal_id: str, *, trigger_actor: str | None = None
) -> dict[str, Any]:
    """Apply the current Care AI approval policy after a successful current v2 assessment."""
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, rs.vertical, rs.source_type, rs.metadata, se.review_status,
                   se.reviewed_by, se.extracted_facts, ai.status, ai.recommendation,
                   ai.confidence, ai.prompt_version
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) ai ON TRUE
            WHERE rs.id = %s
            FOR UPDATE OF se
            """,
            (signal_id,),
        ).fetchone()
        if row is None:
            return {"signal_id": signal_id, "outcome": "NOT_ELIGIBLE", "updated": False}
        fields = (
            "id",
            "vertical",
            "source_type",
            "metadata",
            "review_status",
            "reviewed_by",
            "extracted_facts",
            "ai_status",
            "ai_recommendation",
            "ai_confidence",
            "ai_prompt_version",
        )
        item = dict(zip(fields, row))
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        outcome = care_planning_ai_approval_outcome(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            reviewed_by=item.get("reviewed_by"),
            metadata=item.get("metadata"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_prompt_version=item.get("ai_prompt_version"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        if outcome == "NOT_ELIGIBLE":
            return {"signal_id": signal_id, "outcome": outcome, "updated": False}
        facts = item.get("extracted_facts") or {}
        canonical_outcome = canonical_planning_outcome(item.get("metadata"))
        qa_bucket = care_planning_ai_approval_qa_bucket(str(item["id"]))
        marker = {
            "policy_version": CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
            "outcome": outcome,
            "qa_bucket": qa_bucket,
            "ai_prompt_version": item.get("ai_prompt_version"),
            "ai_recommendation": item.get("ai_recommendation"),
            "ai_confidence": confidence,
            "planning_subtype": facts.get("planning_subtype"),
            "planning_outcome": canonical_outcome.outcome.value,
            "planning_outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
            "source_type": item["source_type"],
            "vertical": item["vertical"],
            "planning_taxonomy_version": facts.get("planning_taxonomy_version"),
            "trigger": "admin_backlog" if trigger_actor else "live_enrichment",
        }
        if outcome == "QA_HOLDOUT":
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                  AND COALESCE(
                        extracted_facts->'care_planning_ai_approval'->>'policy_version', ''
                      ) <> %s
                RETURNING raw_signal_id
                """,
                (
                    Jsonb({"care_planning_ai_approval": marker}),
                    signal_id,
                    CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
                ),
            ).fetchone()
            action = "CARE_PLANNING_AI_APPROVAL_QA_HOLDOUT"
        else:
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET review_status = 'APPROVED', reviewed_by = %s, reviewed_at = now(),
                    extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                RETURNING raw_signal_id
                """,
                (
                    f"system:{CARE_PLANNING_AI_APPROVAL_POLICY_VERSION}",
                    Jsonb({"care_planning_ai_approval": marker}),
                    signal_id,
                ),
            ).fetchone()
            action = "CARE_PLANNING_AI_APPROVAL_AUTO_APPROVE"
        if updated:
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (action, actor, target_type, target_count, details, vertical)
                VALUES (%s, %s, 'signal', 1, %s, 'CHILDRENS_HOME')
                """,
                (
                    action,
                    trigger_actor or f"system:{CARE_PLANNING_AI_APPROVAL_POLICY_VERSION}",
                    Jsonb(
                        {
                            "signal_id": signal_id,
                            "reason": (
                                "Auto-approved — explicit new-home Planning evidence and "
                                "high-confidence current AI assessment agreed."
                                if outcome == "AUTO_APPROVE"
                                else "QA holdout — otherwise qualifies for Care AI approval."
                            ),
                            **marker,
                        }
                    ),
                ),
            )
            conn.commit()
        return {"signal_id": signal_id, "outcome": outcome, "updated": bool(updated)}


def care_planning_ai_approval_backlog(
    settings: Settings,
    *,
    actor: str,
    preview: bool,
    limit: int = 100,
    taxonomy_catchup_only: bool = False,
) -> dict[str, Any]:
    """Preview or process one bounded batch under the current Care AI approval policy."""
    bounded_limit = min(max(int(limit), 1), 100)
    if preview:
        return care_planning_ai_approval_preview(
            settings,
            limit=bounded_limit,
            taxonomy_catchup_only=taxonomy_catchup_only,
        )
    items = _care_planning_ai_approval_rows(settings)
    eligible, _, _ = _care_planning_ai_approval_evaluation(items)
    if taxonomy_catchup_only:
        eligible = [item for item in eligible if _taxonomy_v2_reclassified_candidate(item[0])]
    batch = eligible[:bounded_limit]
    with connection(settings) as conn:
        published_before = conn.execute(
            """SELECT count(*) FROM opportunities
               WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
        ).fetchone()[0]
    results: list[dict[str, Any]] = []
    failures = 0
    for item, _ in batch:
        try:
            results.append(
                apply_care_planning_ai_approval_policy(
                    settings, str(item["id"]), trigger_actor=actor
                )
            )
        except Exception:
            failures += 1
    approved_ids = [
        result["signal_id"]
        for result in results
        if result.get("updated") and result.get("outcome") == "AUTO_APPROVE"
    ]
    opportunities_reused = 0
    relationships_reused = 0
    published_after = 0
    with connection(settings) as conn:
        if approved_ids:
            opportunities_reused, relationships_reused = conn.execute(
                """
                SELECT count(DISTINCT os.opportunity_id), count(*)
                FROM opportunity_signals os
                WHERE os.raw_signal_id = ANY(%s::uuid[]) AND os.status = 'ACTIVE'
                """,
                (approved_ids,),
            ).fetchone()
        published_after = conn.execute(
            """SELECT count(*) FROM opportunities
               WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
        ).fetchone()[0]
    return {
        "preview": False,
        "policy_version": CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
        "requested": len(batch),
        "selected": len(batch),
        "processed": len(results),
        "updated": sum(bool(item.get("updated")) for item in results),
        "auto_approved": len(approved_ids),
        "qa_holdouts": sum(
            item.get("updated") and item.get("outcome") == "QA_HOLDOUT" for item in results
        ),
        "idempotent_skips": sum(not item.get("updated") for item in results),
        "failures": failures,
        "opportunities_created": 0,
        "opportunities_reused": int(opportunities_reused),
        "relationships_created": 0,
        "relationships_reused": int(relationships_reused),
        "match_reviews_created": 0,
        "qa_holdout_references": [
            {
                "signal_id": item.get("signal_id"),
                "external_id": next(
                    (
                        candidate.get("external_id")
                        for candidate, _ in batch
                        if str(candidate["id"]) == item.get("signal_id")
                    ),
                    None,
                ),
            }
            for item in results
            if item.get("updated") and item.get("outcome") == "QA_HOLDOUT"
        ],
        "published_before": int(published_before),
        "published_after": int(published_after),
        "customer_publication_unchanged": True,
        "taxonomy_catchup_only": taxonomy_catchup_only,
    }


def _care_planning_lawfulness_evaluation(
    items: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], str]], dict[str, int]]:
    eligible: list[tuple[dict[str, Any], str]] = []
    exclusions: dict[str, int] = {}
    for item in items:
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        reason = care_planning_lawfulness_exclusion(
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            reviewed_by=item.get("reviewed_by"),
            metadata=item.get("metadata"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_prompt_version=item.get("ai_prompt_version"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        eligible.append(
            (
                item,
                care_planning_lawfulness_outcome(
                    signal_id=str(item["id"]),
                    vertical=item["vertical"],
                    source_type=item["source_type"],
                    review_status=item["review_status"],
                    reviewed_by=item.get("reviewed_by"),
                    metadata=item.get("metadata"),
                    extracted_facts=item.get("extracted_facts"),
                    ai_status=item.get("ai_status"),
                    ai_prompt_version=item.get("ai_prompt_version"),
                    ai_recommendation=item.get("ai_recommendation"),
                    ai_confidence=confidence,
                ),
            )
        )
    return eligible, exclusions


def care_planning_lawfulness_preview(
    settings: Settings, *, taxonomy_catchup_only: bool = False
) -> dict[str, Any]:
    """Preview proposed-lawfulness v1 over the bounded pending Planning cohort."""
    items = _care_planning_ai_approval_rows(settings)
    eligible, exclusions = _care_planning_lawfulness_evaluation(items)
    if taxonomy_catchup_only:
        eligible = [item for item in eligible if _taxonomy_v2_reclassified_candidate(item[0])]
    with connection(settings) as conn:
        published = int(
            conn.execute(
                """SELECT count(*) FROM opportunities
                   WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
            ).fetchone()[0]
        )
    return {
        "preview": True,
        "policy_version": CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
        "prompt_version": CARE_PLANNING_LAWFULNESS_PROMPT_VERSION,
        "confidence_threshold": CARE_PLANNING_LAWFULNESS_MIN_CONFIDENCE,
        "qa_target_percent": 10,
        "pending_evaluated": len(items),
        "eligible": len(eligible),
        "would_auto_approve": sum(outcome == "AUTO_APPROVE" for _, outcome in eligible),
        "qa_holdouts": sum(outcome == "QA_HOLDOUT" for _, outcome in eligible),
        "exclusions": dict(sorted(exclusions.items())),
        "published_opportunities": published,
        "customer_publication_unchanged": True,
        "taxonomy_catchup_only": taxonomy_catchup_only,
    }


def apply_care_planning_lawfulness_policy(
    settings: Settings, signal_id: str, *, trigger_actor: str | None = None
) -> dict[str, Any]:
    """Apply proposed-lawfulness v1 once after a successful current v2 assessment."""
    with connection(settings) as conn:
        row = conn.execute(
            """
            SELECT rs.id, rs.vertical, rs.source_type, rs.metadata, se.review_status,
                   se.reviewed_by, se.extracted_facts, ai.status, ai.recommendation,
                   ai.confidence, ai.prompt_version
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            LEFT JOIN LATERAL (
                SELECT status, recommendation, confidence, prompt_version
                FROM signal_ai_reviews
                WHERE raw_signal_id = rs.id
                ORDER BY created_at DESC, id DESC LIMIT 1
            ) ai ON TRUE
            WHERE rs.id = %s
            FOR UPDATE OF se
            """,
            (signal_id,),
        ).fetchone()
        if row is None:
            return {"signal_id": signal_id, "outcome": "NOT_ELIGIBLE", "updated": False}
        fields = (
            "id",
            "vertical",
            "source_type",
            "metadata",
            "review_status",
            "reviewed_by",
            "extracted_facts",
            "ai_status",
            "ai_recommendation",
            "ai_confidence",
            "ai_prompt_version",
        )
        item = dict(zip(fields, row))
        confidence = float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
        outcome = care_planning_lawfulness_outcome(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            reviewed_by=item.get("reviewed_by"),
            metadata=item.get("metadata"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_prompt_version=item.get("ai_prompt_version"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=confidence,
        )
        if outcome == "NOT_ELIGIBLE":
            return {"signal_id": signal_id, "outcome": outcome, "updated": False}
        facts = item.get("extracted_facts") or {}
        canonical_outcome = canonical_planning_outcome(item.get("metadata"))
        marker = {
            "policy_version": CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
            "outcome": outcome,
            "qa_bucket": care_planning_lawfulness_qa_bucket(str(item["id"])),
            "ai_prompt_version": item.get("ai_prompt_version"),
            "ai_recommendation": item.get("ai_recommendation"),
            "ai_confidence": confidence,
            "planning_subtype": facts.get("planning_subtype"),
            "planning_outcome": canonical_outcome.outcome.value,
            "planning_outcome_policy_version": PLANNING_OUTCOME_POLICY_VERSION,
            "opportunity_creation_decision": facts.get("opportunity_creation_decision"),
            "source_type": item["source_type"],
            "vertical": item["vertical"],
            "planning_taxonomy_version": facts.get("planning_taxonomy_version"),
            "trigger": "admin" if trigger_actor else "live_enrichment",
        }
        marker_json = Jsonb({"care_planning_lawfulness_approval": marker})
        if outcome == "QA_HOLDOUT":
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                  AND COALESCE(
                    extracted_facts->'care_planning_lawfulness_approval'->>'policy_version', ''
                  ) <> %s
                RETURNING raw_signal_id
                """,
                (marker_json, signal_id, CARE_PLANNING_LAWFULNESS_POLICY_VERSION),
            ).fetchone()
            action = "CARE_PLANNING_LAWFULNESS_QA_HOLDOUT"
        else:
            updated = conn.execute(
                """
                UPDATE signal_enrichments
                SET review_status = 'APPROVED', reviewed_by = %s, reviewed_at = now(),
                    extracted_facts = extracted_facts || %s, updated_at = now()
                WHERE raw_signal_id = %s AND review_status = 'PENDING'
                RETURNING raw_signal_id
                """,
                (
                    f"system:{CARE_PLANNING_LAWFULNESS_POLICY_VERSION}",
                    marker_json,
                    signal_id,
                ),
            ).fetchone()
            action = "CARE_PLANNING_LAWFULNESS_AUTO_APPROVE"
        if updated:
            conn.execute(
                """
                INSERT INTO admin_audit_events
                    (action, actor, target_type, target_count, details, vertical)
                VALUES (%s, %s, 'signal', 1, %s, 'CHILDRENS_HOME')
                """,
                (
                    action,
                    trigger_actor or f"system:{CARE_PLANNING_LAWFULNESS_POLICY_VERSION}",
                    Jsonb(
                        {
                            "signal_id": signal_id,
                            "reason": (
                                "Auto-approved — proposed new-home lawfulness evidence and "
                                "high-confidence current AI assessment agreed."
                                if outcome == "AUTO_APPROVE"
                                else (
                                    "QA holdout — otherwise qualifies for "
                                    "proposed-lawfulness approval."
                                )
                            ),
                            **marker,
                        }
                    ),
                ),
            )
            conn.commit()
        return {"signal_id": signal_id, "outcome": outcome, "updated": bool(updated)}


def care_planning_lawfulness_backlog(
    settings: Settings,
    *,
    actor: str,
    preview: bool,
    limit: int = 100,
    taxonomy_catchup_only: bool = False,
) -> dict[str, Any]:
    """Preview or process one bounded proposed-lawfulness batch using the live policy."""
    bounded_limit = min(max(int(limit), 1), 100)
    if preview:
        return care_planning_lawfulness_preview(
            settings, taxonomy_catchup_only=taxonomy_catchup_only
        )
    items = _care_planning_ai_approval_rows(settings)
    eligible, _ = _care_planning_lawfulness_evaluation(items)
    if taxonomy_catchup_only:
        eligible = [item for item in eligible if _taxonomy_v2_reclassified_candidate(item[0])]
    batch = eligible[:bounded_limit]
    with connection(settings) as conn:
        published_before = int(
            conn.execute(
                """SELECT count(*) FROM opportunities
                   WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
            ).fetchone()[0]
        )
    results: list[dict[str, Any]] = []
    failures = 0
    for item, _ in batch:
        try:
            results.append(
                apply_care_planning_lawfulness_policy(
                    settings, str(item["id"]), trigger_actor=actor
                )
            )
        except Exception:
            failures += 1
    approved_ids = [
        result["signal_id"]
        for result in results
        if result.get("updated") and result.get("outcome") == "AUTO_APPROVE"
    ]
    opportunities_reused = 0
    relationships_reused = 0
    with connection(settings) as conn:
        if approved_ids:
            opportunities_reused, relationships_reused = conn.execute(
                """
                SELECT count(DISTINCT os.opportunity_id), count(*)
                FROM opportunity_signals os
                WHERE os.raw_signal_id = ANY(%s::uuid[]) AND os.status = 'ACTIVE'
                """,
                (approved_ids,),
            ).fetchone()
        published_after = int(
            conn.execute(
                """SELECT count(*) FROM opportunities
                   WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
            ).fetchone()[0]
        )
    return {
        "preview": False,
        "policy_version": CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
        "selected": len(batch),
        "processed": len(results),
        "updated": sum(bool(item.get("updated")) for item in results),
        "auto_approved": len(approved_ids),
        "qa_holdouts": sum(
            item.get("updated") and item.get("outcome") == "QA_HOLDOUT" for item in results
        ),
        "idempotent_skips": sum(not item.get("updated") for item in results),
        "failures": failures,
        "opportunities_created": 0,
        "opportunities_reused": int(opportunities_reused),
        "relationships_created": 0,
        "relationships_reused": int(relationships_reused),
        "match_reviews_created": 0,
        "qa_holdout_references": [
            {
                "signal_id": item.get("signal_id"),
                "external_id": next(
                    (
                        candidate.get("external_id")
                        for candidate, _ in batch
                        if str(candidate["id"]) == item.get("signal_id")
                    ),
                    None,
                ),
            }
            for item in results
            if item.get("updated") and item.get("outcome") == "QA_HOLDOUT"
        ],
        "published_before": published_before,
        "published_after": published_after,
        "customer_publication_unchanged": published_before == published_after,
        "taxonomy_catchup_only": taxonomy_catchup_only,
    }


CARE_PLANNING_MANUAL_ANALYSIS_SUBTYPES = (
    "NEW_HOME_CHANGE_OF_USE",
    "NEW_HOME_OTHER_EXPLICIT",
    "NEW_HOME_MIXED_USE",
    "LAWFULNESS_PROPOSED",
    "LAWFULNESS_EXISTING",
    "EXPANSION_OR_CAPACITY_CHANGE",
    "CONDITION_VARIATION",
    "CONDITION_DISCHARGE",
    "NON_MATERIAL_AMENDMENT",
    "FOLLOW_UP_OTHER",
    "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
    "AMBIGUOUS",
)


def _counter_payload(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def care_planning_manual_analysis_from_rows(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the bounded, read-only manual-cohort and lawfulness policy preview."""
    subtype_rows: dict[str, list[dict[str, Any]]] = {
        subtype: [] for subtype in CARE_PLANNING_MANUAL_ANALYSIS_SUBTYPES
    }
    for item in items:
        facts = item.get("extracted_facts") or {}
        subtype = str(facts.get("planning_subtype") or "AMBIGUOUS")
        subtype_rows.setdefault(subtype, []).append(item)

    subtype_analysis: dict[str, Any] = {}
    for subtype, rows in subtype_rows.items():
        recommendations = [str(item.get("ai_recommendation") or "MISSING") for item in rows]
        confidences = [
            f"{float(item['ai_confidence']):.2f}"
            if item.get("ai_confidence") is not None
            else "MISSING"
            for item in rows
        ]
        triage = [
            review_triage_bucket(
                signal_id=str(item["id"]),
                vertical=item["vertical"],
                source_type=item["source_type"],
                review_status=item["review_status"],
                metadata=item.get("metadata"),
                extracted_facts=item.get("extracted_facts"),
                ai_status=item.get("ai_status"),
                ai_recommendation=item.get("ai_recommendation"),
                ai_confidence=(
                    float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
                ),
            )
            for item in rows
        ]
        subtype_analysis[subtype] = {
            "total_pending": len(rows),
            "ai_recommendations": _counter_payload(recommendations),
            "confidence_distribution": _counter_payload(confidences),
            "rule_ai_disagreement": triage.count("DETERMINISTIC_AI_DISAGREE"),
            "manual_review": triage.count("MANUAL_REVIEW_REQUIRED"),
            "triage_distribution": _counter_payload(triage),
            "canonical_outcomes": _counter_payload(
                [canonical_planning_outcome(item.get("metadata")).outcome.value for item in rows]
            ),
            "opportunity_creation_decisions": _counter_payload(
                [
                    str(
                        (item.get("extracted_facts") or {}).get("opportunity_creation_decision")
                        or "MISSING"
                    )
                    for item in rows
                ]
            ),
            "opportunity_change_types": _counter_payload(
                [
                    str(
                        (item.get("extracted_facts") or {}).get("opportunity_change_type")
                        or "MISSING"
                    )
                    for item in rows
                ]
            ),
        }

    lawfulness = subtype_rows.get("LAWFULNESS_PROPOSED", [])
    lawfulness_exclusions: Counter[str] = Counter()
    lawfulness_eligible: list[dict[str, Any]] = []
    for item in lawfulness:
        facts = item.get("extracted_facts") or {}
        outcome = canonical_planning_outcome(item.get("metadata")).outcome
        marker_present = any(
            isinstance(facts.get(marker), dict)
            for marker in (
                "automatic_review",
                "safe_approval",
                "care_planning_fastpath",
                "care_planning_ai_approval",
            )
        )
        if item.get("reviewed_by") or marker_present:
            reason = "EXISTING_REVIEW_OR_POLICY_STATE"
        elif item.get("ai_status") != "SUCCEEDED" or (
            item.get("ai_prompt_version") != CARE_PLANNING_AI_APPROVAL_PROMPT_VERSION
        ):
            reason = "AI_VERSION_OR_STATUS"
        elif item.get("ai_recommendation") != "APPROVE":
            reason = "AI_RECOMMENDATION"
        elif float(item.get("ai_confidence") or 0) < CARE_PLANNING_AI_APPROVAL_MIN_CONFIDENCE:
            reason = "AI_CONFIDENCE"
        elif facts.get("explicit_new_home_proposal") is not True:
            reason = "NO_EXPLICIT_NEW_HOME_WORDING"
        elif outcome in CARE_PLANNING_AI_APPROVAL_BLOCKED_OUTCOMES:
            reason = "PLANNING_OUTCOME"
        elif facts.get("likely_false_positive") is True:
            reason = "LIKELY_FALSE_POSITIVE"
        elif facts.get("planning_ambiguity_markers"):
            reason = "AMBIGUITY"
        else:
            reason = None
        if reason:
            lawfulness_exclusions[reason] += 1
        else:
            lawfulness_eligible.append(item)

    lawfulness_holdouts = sum(
        care_planning_fastpath_qa_holdout(str(item["id"])) for item in lawfulness_eligible
    )
    lawfulness_facts = [item.get("extracted_facts") or {} for item in lawfulness]
    lawfulness_detail = {
        **subtype_analysis["LAWFULNESS_PROPOSED"],
        "explicit_new_home_wording": sum(
            facts.get("explicit_new_home_proposal") is True for facts in lawfulness_facts
        ),
        "likely_false_positive": sum(
            facts.get("likely_false_positive") is True for facts in lawfulness_facts
        ),
        "ambiguity_markers": sum(
            bool(facts.get("planning_ambiguity_markers")) for facts in lawfulness_facts
        ),
        "prior_application_references": sum(
            bool(facts.get("planning_prior_references")) for facts in lawfulness_facts
        ),
        "active_appeals": sum(
            canonical_planning_outcome(item.get("metadata")).outcome
            is PlanningOutcome.REFUSED_UNDER_APPEAL
            for item in lawfulness
        ),
        "malformed_or_missing_ai": sum(
            item.get("ai_status") != "SUCCEEDED" or not item.get("ai_prompt_version")
            for item in lawfulness
        ),
    }
    return {
        "pending_total": len(items),
        "subtypes": subtype_analysis,
        "lawfulness_proposed": lawfulness_detail,
        "lawfulness_policy_preview": {
            "policy_name": "care-planning-lawfulness-proposed-v1",
            "enabled": False,
            "eligible": len(lawfulness_eligible),
            "would_auto_approve": len(lawfulness_eligible) - lawfulness_holdouts,
            "qa_holdouts_at_10_percent": lawfulness_holdouts,
            "excluded_reasons": dict(sorted(lawfulness_exclusions.items())),
        },
        "no_new_subtype_automation_enabled": True,
        "customer_publication_unchanged": True,
    }


def care_planning_manual_cohort_analysis(settings: Settings) -> dict[str, Any]:
    """Return a bounded production analysis without mutating reviews or opportunities."""
    items = _care_planning_ai_approval_rows(settings)
    report = care_planning_manual_analysis_from_rows(items)
    with connection(settings) as conn:
        report["published_opportunities"] = int(
            conn.execute(
                """SELECT count(*) FROM opportunities
                   WHERE vertical = 'CHILDRENS_HOME' AND publication_status = 'PUBLISHED'"""
            ).fetchone()[0]
        )
        report["unsupported_draft_opportunities"] = int(
            conn.execute(
                """
                SELECT count(DISTINCT o.id)
                FROM opportunities o
                WHERE o.vertical = 'CHILDRENS_HOME' AND o.publication_status = 'DRAFT'
                  AND EXISTS (
                    SELECT 1 FROM opportunity_signals os
                    JOIN signal_enrichments se ON se.raw_signal_id = os.raw_signal_id
                    WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                      AND se.extracted_facts->>'planning_subtype' IN
                        ('REFUSED', 'WITHDRAWN', 'LAWFULNESS_EXISTING',
                         'CONDITION_DISCHARGE', 'NON_MATERIAL_AMENDMENT', 'FOLLOW_UP_OTHER')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM opportunity_signals os
                    JOIN signal_enrichments se ON se.raw_signal_id = os.raw_signal_id
                    WHERE os.opportunity_id = o.id AND os.status = 'ACTIVE'
                      AND COALESCE(se.extracted_facts->>'planning_subtype', '') NOT IN
                        ('REFUSED', 'WITHDRAWN', 'LAWFULNESS_EXISTING',
                         'CONDITION_DISCHARGE', 'NON_MATERIAL_AMENDMENT', 'FOLLOW_UP_OTHER')
                      AND se.review_status <> 'REJECTED'
                  )
                """
            ).fetchone()[0]
        )
    return report


def cleanup_refused_planning_signals(
    settings: Settings, *, actor: str, limit: int = 2000, vertical: str = "ALL"
) -> dict[str, Any]:
    """Boundedly remove explicit structured refusals from the pending review inbox."""
    bounded_limit = min(max(int(limit), 1), 2500)
    selected_vertical = validate_vertical_filter(vertical)
    vertical_clause = "" if selected_vertical == "ALL" else " AND rs.vertical = %s"
    row_params: list[Any] = []
    if selected_vertical != "ALL":
        row_params.append(selected_vertical)
    row_params.append(bounded_limit)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT rs.id, rs.metadata, rs.vertical, se.review_status
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.source_type = 'planning' AND se.review_status = 'PENDING'
              {vertical_clause}
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            row_params,
        ).fetchall()
        reviewed_rows = conn.execute(
            f"""
            SELECT rs.metadata
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.source_type = 'planning'
              AND se.review_status IN ('APPROVED', 'REJECTED')
              {vertical_clause}
            """,
            row_params[:-1],
        ).fetchall()
    refused = [
        (str(row[0]), row[1] or {}, str(row[2]))
        for row in rows
        if planning_refusal_assessment(row[1]).refused
    ]
    ambiguous_counts: dict[str, int] = {}
    terminal_terms = {"WITHDRAWN", "INVALID", "RETURNED", "LAPSED", "EXPIRED"}
    for _, raw_metadata, *_ in rows:
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        provider_record = metadata.get("provider_record")
        provider_record = provider_record if isinstance(provider_record, dict) else {}
        provider_decision = provider_record.get("decision")
        provider_decision = provider_decision if isinstance(provider_decision, dict) else {}
        values = [
            metadata.get("decision"),
            metadata.get("planning_status"),
            provider_record.get("status"),
            provider_decision.get("outcome"),
        ]
        for value in values:
            normalized = str(value or "").upper().strip(" .:-")
            if normalized in terminal_terms:
                ambiguous_counts[normalized] = ambiguous_counts.get(normalized, 0) + 1
                break
    updated = 0
    errors = 0
    with connection(settings) as conn:
        for signal_id, metadata, _vertical in refused:
            try:
                with conn.transaction():
                    updated += int(
                        _auto_reject_refused_planning_with_connection(
                            conn,
                            signal_id,
                            metadata,
                            actor=f"system:{REFUSAL_POLICY_VERSION}",
                        )
                    )
            except Exception:
                errors += 1
        conn.execute(
            """
            INSERT INTO admin_audit_events
                (action, actor, target_type, target_count, details)
            VALUES ('PLANNING_REFUSAL_CLEANUP', %s, 'signal', %s, %s)
            """,
            (
                actor,
                updated,
                Jsonb(
                    {
                        "policy_version": REFUSAL_POLICY_VERSION,
                        "inspected": len(rows),
                        "explicit_refusals_found": len(refused),
                        "auto_rejected": updated,
                        "errors": errors,
                        "vertical": selected_vertical,
                    }
                ),
            ),
        )
        conn.commit()
    refused_ids = [item[0] for item in refused]
    associated_drafts = unsupported_drafts = 0
    if refused_ids:
        with connection(settings) as conn:
            associated_drafts = conn.execute(
                """
                SELECT count(DISTINCT o.id)
                FROM opportunities o
                JOIN opportunity_signals os ON os.opportunity_id = o.id
                WHERE os.raw_signal_id = ANY(%s::uuid[]) AND os.status = 'ACTIVE'
                  AND o.publication_status = 'DRAFT'
                """,
                (refused_ids,),
            ).fetchone()[0]
            unsupported_drafts = conn.execute(
                """
                SELECT count(DISTINCT o.id)
                FROM opportunities o
                JOIN opportunity_signals refused_os ON refused_os.opportunity_id = o.id
                WHERE refused_os.raw_signal_id = ANY(%s::uuid[])
                  AND refused_os.status = 'ACTIVE' AND o.publication_status = 'DRAFT'
                  AND NOT EXISTS (
                      SELECT 1 FROM opportunity_signals support_os
                      JOIN signal_enrichments support_se
                        ON support_se.raw_signal_id = support_os.raw_signal_id
                      WHERE support_os.opportunity_id = o.id
                        AND support_os.status = 'ACTIVE'
                        AND support_se.review_status <> 'REJECTED'
                  )
                """,
                (refused_ids,),
            ).fetchone()[0]
    return {
        "policy_version": REFUSAL_POLICY_VERSION,
        "vertical": selected_vertical,
        "pending_planning_inspected": len(rows),
        "inspection_limit": bounded_limit,
        "explicit_refusals_found": len(refused),
        "auto_rejected": updated,
        "skipped_ambiguous": sum(ambiguous_counts.values()),
        "ambiguous_statuses": ambiguous_counts,
        "already_reviewed": sum(
            1 for row in reviewed_rows if planning_refusal_assessment(row[0]).refused
        ),
        "associated_draft_opportunities": associated_drafts,
        "unsupported_draft_opportunities": unsupported_drafts,
        "errors": errors,
    }


def _review_triage_rows(
    conn: Any, review_status: str, vertical: str = "ALL"
) -> list[dict[str, Any]]:
    vertical = validate_vertical_filter(vertical)
    vertical_clause = "" if vertical == "ALL" else " AND rs.vertical = %s"
    params: list[Any] = [review_status]
    if vertical != "ALL":
        params.append(vertical)
    rows = conn.execute(
        f"""
        SELECT rs.id, rs.title, rs.discovered_at, rs.vertical, rs.source_type, rs.metadata,
               se.review_status,
               se.extracted_facts, ai.status, ai.recommendation, ai.confidence,
               ai.prompt_version, se.reviewed_by, se.reviewed_at
        FROM raw_signals rs
        JOIN signal_enrichments se ON se.raw_signal_id = rs.id
        LEFT JOIN LATERAL (
            SELECT status, recommendation, confidence, prompt_version
            FROM signal_ai_reviews
            WHERE raw_signal_id = rs.id
            ORDER BY created_at DESC, id DESC LIMIT 1
        ) ai ON TRUE
        WHERE rs.source_type = 'planning'
          AND se.review_status = %s
          {vertical_clause}
        ORDER BY rs.discovered_at DESC, rs.id DESC
        LIMIT 5000
        """,
        params,
    ).fetchall()
    fields = (
        "id",
        "title",
        "discovered_at",
        "vertical",
        "source_type",
        "metadata",
        "review_status",
        "extracted_facts",
        "ai_status",
        "ai_recommendation",
        "ai_confidence",
        "ai_prompt_version",
        "reviewed_by",
        "reviewed_at",
    )
    return [dict(zip(fields, row)) for row in rows]


def review_triage_summary(settings: Settings, *, vertical: str = "ALL") -> dict[str, Any]:
    """Evaluate human labels and return bounded pending review buckets."""
    vertical = validate_vertical_filter(vertical)
    with connection(settings) as conn:
        reviewed_all = _review_triage_rows(conn, "APPROVED", vertical) + _review_triage_rows(
            conn, "REJECTED", vertical
        )
        pending = _review_triage_rows(conn, "PENDING", vertical)
    reviewed = [
        item
        for item in reviewed_all
        if not str(item.get("reviewed_by") or "").startswith("system:")
    ]

    def metrics_for(vertical: str) -> dict[str, Any]:
        items = [item for item in reviewed if item["vertical"] == vertical]
        ai_items = [
            item
            for item in items
            if item["ai_status"] == "SUCCEEDED"
            and item["ai_recommendation"] in {"APPROVE", "REJECT"}
        ]
        deterministic_items = [
            item
            for item in items
            if deterministic_review_recommendation(item["extracted_facts"]) in {"APPROVE", "REJECT"}
        ]
        ai_agree = sum(
            (item["ai_recommendation"] == "APPROVE") == (item["review_status"] == "APPROVED")
            for item in ai_items
        )
        deterministic_agree = sum(
            (deterministic_review_recommendation(item["extracted_facts"]) == "APPROVE")
            == (item["review_status"] == "APPROVED")
            for item in deterministic_items
        )
        ai_deterministic = [
            item
            for item in ai_items
            if deterministic_review_recommendation(item["extracted_facts"]) in {"APPROVE", "REJECT"}
        ]
        threshold_tradeoff = []
        for threshold in (0.9, 0.95, 0.97, 0.99):
            cohort = [
                item
                for item in ai_items
                if item["ai_recommendation"] == "APPROVE"
                and deterministic_review_recommendation(item["extracted_facts"]) == "APPROVE"
                and not planning_refusal_assessment(item["metadata"]).refused
                and float(item["ai_confidence"] or 0) >= threshold
            ]
            errors = sum(item["review_status"] != "APPROVED" for item in cohort)
            threshold_tradeoff.append(
                {"threshold": threshold, "cohort_size": len(cohort), "errors": errors}
            )
        return {
            "human_reviewed": len(items),
            "ai_evaluated": len(ai_items),
            "ai_agreement_count": ai_agree,
            "ai_agreement_rate": ai_agree / len(ai_items) if ai_items else None,
            "deterministic_evaluated": len(deterministic_items),
            "deterministic_agreement_count": deterministic_agree,
            "deterministic_agreement_rate": (
                deterministic_agree / len(deterministic_items) if deterministic_items else None
            ),
            "ai_deterministic_agreement_rate": (
                sum(
                    item["ai_recommendation"]
                    == deterministic_review_recommendation(item["extracted_facts"])
                    for item in ai_deterministic
                )
                / len(ai_deterministic)
                if ai_deterministic
                else None
            ),
            "false_approve_count": sum(
                item["ai_recommendation"] == "APPROVE" and item["review_status"] == "REJECTED"
                for item in ai_items
            ),
            "false_reject_count": sum(
                item["ai_recommendation"] == "REJECT" and item["review_status"] == "APPROVED"
                for item in ai_items
            ),
            "disagreement_count": sum(
                item["ai_recommendation"]
                != deterministic_review_recommendation(item["extracted_facts"])
                for item in ai_deterministic
            ),
            "threshold_tradeoff": threshold_tradeoff,
        }

    combined_tradeoff = []
    reviewed_ai = [
        item
        for item in reviewed
        if item["vertical"] == "NURSERY"
        and item["ai_status"] == "SUCCEEDED"
        and item["ai_recommendation"] in {"APPROVE", "REJECT"}
    ]
    for threshold in (0.95, 0.97, 0.99):
        cohort = [
            item
            for item in reviewed_ai
            if item["ai_recommendation"] == "APPROVE"
            and deterministic_review_recommendation(item["extracted_facts"]) == "APPROVE"
            and not planning_refusal_assessment(item["metadata"]).refused
            and float(item["ai_confidence"] or 0) >= threshold
        ]
        errors = sum(item["review_status"] != "APPROVED" for item in cohort)
        combined_tradeoff.append(
            {"threshold": threshold, "cohort_size": len(cohort), "errors": errors}
        )
    safe_threshold = next(
        (
            item["threshold"]
            for item in combined_tradeoff
            if item["cohort_size"] >= 20 and item["errors"] == 0
        ),
        None,
    )
    threshold = float(safe_threshold or SAFE_APPROVAL_MIN_CONFIDENCE)
    buckets: dict[str, int] = {}
    safe_ids: list[str] = []
    for item in pending:
        bucket = review_triage_bucket(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item["metadata"],
            extracted_facts=item["extracted_facts"],
            ai_status=item["ai_status"],
            ai_recommendation=item["ai_recommendation"],
            ai_confidence=(float(item["ai_confidence"]) if item["ai_confidence"] else None),
            threshold=threshold,
        )
        buckets[bucket] = buckets.get(bucket, 0) + 1
        if bucket in {"SAFE_APPROVE_AGREEMENT", "QA_HOLDOUT_SAFE_AGREEMENT"}:
            safe_ids.append(str(item["id"]))
    policy_records = []
    for item in reviewed_all + pending:
        facts = item.get("extracted_facts") or {}
        marker = facts.get("safe_approval") if isinstance(facts, dict) else None
        if (
            isinstance(marker, dict)
            and marker.get("policy_version") == SAFE_APPROVAL_POLICY_VERSION
        ):
            policy_records.append((item, marker))
    holdouts = [
        (item, marker) for item, marker in policy_records if marker.get("outcome") == "QA_HOLDOUT"
    ]
    holdout_rejected = sum(item["review_status"] == "REJECTED" for item, _ in holdouts)
    care_policy_records = []
    for item in reviewed_all + pending:
        facts = item.get("extracted_facts") or {}
        marker = facts.get("care_planning_fastpath") if isinstance(facts, dict) else None
        if (
            isinstance(marker, dict)
            and marker.get("policy_version") == CARE_PLANNING_FASTPATH_POLICY_VERSION
        ):
            care_policy_records.append((item, marker))
    care_holdouts = [
        (item, marker)
        for item, marker in care_policy_records
        if marker.get("outcome") == "QA_HOLDOUT"
    ]
    care_holdout_rejected = sum(item["review_status"] == "REJECTED" for item, _ in care_holdouts)
    care_ai_policy_records = []
    for item in reviewed_all + pending:
        facts = item.get("extracted_facts") or {}
        marker = facts.get("care_planning_ai_approval") if isinstance(facts, dict) else None
        if (
            isinstance(marker, dict)
            and marker.get("policy_version") in CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS
        ):
            care_ai_policy_records.append((item, marker))
    care_ai_holdouts = [
        (item, marker)
        for item, marker in care_ai_policy_records
        if marker.get("outcome") == "QA_HOLDOUT"
    ]
    care_ai_holdout_rejected = sum(
        item["review_status"] == "REJECTED" for item, _ in care_ai_holdouts
    )
    care_lawfulness_records = []
    for item in reviewed_all + pending:
        facts = item.get("extracted_facts") or {}
        marker = facts.get("care_planning_lawfulness_approval") if isinstance(facts, dict) else None
        if (
            isinstance(marker, dict)
            and marker.get("policy_version") == CARE_PLANNING_LAWFULNESS_POLICY_VERSION
        ):
            care_lawfulness_records.append((item, marker))
    care_lawfulness_holdouts = [
        (item, marker)
        for item, marker in care_lawfulness_records
        if marker.get("outcome") == "QA_HOLDOUT"
    ]
    care_lawfulness_rejected = sum(
        item["review_status"] == "REJECTED" for item, _ in care_lawfulness_holdouts
    )
    care_ai_versions: dict[str, dict[str, int]] = {}
    for item, marker in care_ai_policy_records:
        version = str(marker.get("policy_version"))
        metrics = care_ai_versions.setdefault(
            version,
            {
                "total": 0,
                "auto_approved": 0,
                "qa_holdouts": 0,
                "qa_approved": 0,
                "qa_rejected": 0,
                "qa_pending": 0,
            },
        )
        metrics["total"] += 1
        if marker.get("outcome") == "AUTO_APPROVE":
            metrics["auto_approved"] += 1
        elif marker.get("outcome") == "QA_HOLDOUT":
            metrics["qa_holdouts"] += 1
            status_key = {
                "APPROVED": "qa_approved",
                "REJECTED": "qa_rejected",
                "PENDING": "qa_pending",
            }.get(item["review_status"])
            if status_key:
                metrics[status_key] += 1
    care_pending = [
        item
        for item in pending
        if item["vertical"] == "CHILDRENS_HOME" and item["source_type"] == "planning"
    ]
    care_ai_currency = {
        key: 0 for key in ("CURRENT_V2", "STALE_V1", "NO_AI_ASSESSMENT", "AI_FAILED")
    }
    for item in care_pending:
        if not item.get("ai_prompt_version"):
            key = "NO_AI_ASSESSMENT"
        elif item.get("ai_status") == "FAILED":
            key = "AI_FAILED"
        elif item["ai_prompt_version"] == settings.ai_care_planning_prompt_version:
            key = "CURRENT_V2"
        else:
            key = "STALE_V1"
        care_ai_currency[key] += 1
    return {
        "policy_version": "review-triage-v2",
        "safe_approval_policy_version": SAFE_APPROVAL_POLICY_VERSION,
        "safe_approval_verticals": sorted(SAFE_APPROVAL_VERTICALS),
        "validation_cohort": {
            "vertical": "NURSERY",
            "authoritative_count": 69,
            "approved": 69,
            "rejected": 0,
            "caveat": (
                "Historical review rows do not snapshot every policy input at decision time; "
                "the cohort is authoritative operator validation, not a perfect policy-time "
                "reconstruction."
            ),
        },
        "vertical": vertical,
        "reviewed_sample_size": len(reviewed),
        "automatic_reviewed_excluded": len(reviewed_all) - len(reviewed),
        "verticals": {
            "NURSERY": metrics_for("NURSERY"),
            "CHILDRENS_HOME": metrics_for("CHILDRENS_HOME"),
        },
        "combined_threshold_tradeoff": combined_tradeoff,
        "recommended_safe_threshold": safe_threshold,
        "safe_bulk_approval_recommended": safe_threshold is not None,
        "pending_planning_evaluated": len(pending),
        "pending_buckets": buckets,
        "care_planning_ai_currency": {
            "prompt_version": settings.ai_care_planning_prompt_version,
            "counts": care_ai_currency,
        },
        "safe_pending_count": len(safe_ids),
        "qa_monitoring": {
            "total_policy_records": len(policy_records),
            "auto_approved": sum(
                marker.get("outcome") == "AUTO_APPROVE" for _, marker in policy_records
            ),
            "qa_holdouts": len(holdouts),
            "qa_holdouts_pending": sum(item["review_status"] == "PENDING" for item, _ in holdouts),
            "qa_holdouts_approved": sum(
                item["review_status"] == "APPROVED" for item, _ in holdouts
            ),
            "qa_holdouts_rejected": holdout_rejected,
            "observed_error_rate": holdout_rejected / len(holdouts) if holdouts else None,
            "warning": (
                "QA holdout rejection detected; review safe-approval-v1 before widening or "
                "continuing automation."
                if holdout_rejected
                else None
            ),
        },
        "care_planning_qa_monitoring": {
            "policy_version": CARE_PLANNING_FASTPATH_POLICY_VERSION,
            "total_policy_records": len(care_policy_records),
            "auto_approved": sum(
                marker.get("outcome") == "AUTO_APPROVE" for _, marker in care_policy_records
            ),
            "qa_holdouts": len(care_holdouts),
            "qa_holdouts_pending": sum(
                item["review_status"] == "PENDING" for item, _ in care_holdouts
            ),
            "qa_holdouts_approved": sum(
                item["review_status"] == "APPROVED" for item, _ in care_holdouts
            ),
            "qa_holdouts_rejected": care_holdout_rejected,
            "observed_error_rate": (
                care_holdout_rejected / len(care_holdouts) if care_holdouts else None
            ),
            "warning": (
                "CareProspect explicit-new-home QA rejection detected; suspend and review "
                "care-planning-fastpath-v1."
                if care_holdout_rejected
                else None
            ),
        },
        "care_planning_ai_approval_monitoring": {
            "policy_version": CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
            "future_qa_target_percent": 5,
            "historical_policy_versions": sorted(CARE_PLANNING_AI_APPROVAL_POLICY_VERSIONS),
            "by_policy_version": care_ai_versions,
            "total_policy_records": len(care_ai_policy_records),
            "auto_approved": sum(
                marker.get("outcome") == "AUTO_APPROVE" for _, marker in care_ai_policy_records
            ),
            "qa_holdouts": len(care_ai_holdouts),
            "qa_holdouts_pending": sum(
                item["review_status"] == "PENDING" for item, _ in care_ai_holdouts
            ),
            "qa_holdouts_approved": sum(
                item["review_status"] == "APPROVED" for item, _ in care_ai_holdouts
            ),
            "qa_holdouts_rejected": care_ai_holdout_rejected,
            "observed_error_rate": (
                care_ai_holdout_rejected / len(care_ai_holdouts) if care_ai_holdouts else None
            ),
            "warning": (
                "CareProspect AI approval QA rejection detected; review "
                "care-planning-ai-approval-v1 before continuing or widening automation."
                if care_ai_holdout_rejected
                else None
            ),
        },
        "care_planning_lawfulness_monitoring": {
            "policy_version": CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
            "historical_validation": {
                "reviewed": 7,
                "approved": 7,
                "rejected": 0,
                "caveat": "Narrow proposed-new-home lawfulness cohort only.",
            },
            "total_policy_records": len(care_lawfulness_records),
            "auto_approved": sum(
                marker.get("outcome") == "AUTO_APPROVE" for _, marker in care_lawfulness_records
            ),
            "qa_holdouts": len(care_lawfulness_holdouts),
            "qa_holdouts_pending": sum(
                item["review_status"] == "PENDING" for item, _ in care_lawfulness_holdouts
            ),
            "qa_holdouts_approved": sum(
                item["review_status"] == "APPROVED" for item, _ in care_lawfulness_holdouts
            ),
            "qa_holdouts_rejected": care_lawfulness_rejected,
            "observed_error_rate": (
                care_lawfulness_rejected / len(care_lawfulness_holdouts)
                if care_lawfulness_holdouts
                else None
            ),
            "warning": (
                "CareProspect proposed-lawfulness QA rejection detected; review "
                "care-planning-lawfulness-proposed-v1 before continuing automation."
                if care_lawfulness_rejected
                else None
            ),
        },
    }


def pending_review_triage_ids(settings: Settings, *, vertical: str, bucket: str) -> list[str]:
    """Return pending Planning IDs in one validated server-side triage bucket."""
    vertical = validate_vertical_filter(vertical)
    bucket = validate_triage_bucket(bucket)
    if bucket is None:
        return []
    summary = review_triage_summary(settings, vertical=vertical)
    threshold = float(summary.get("recommended_safe_threshold") or SAFE_APPROVAL_MIN_CONFIDENCE)
    with connection(settings) as conn:
        pending = _review_triage_rows(conn, "PENDING", vertical)
    return [
        str(item["id"])
        for item in pending
        if review_triage_bucket(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item["metadata"],
            extracted_facts=item["extracted_facts"],
            ai_status=item["ai_status"],
            ai_recommendation=item["ai_recommendation"],
            ai_confidence=(
                float(item["ai_confidence"]) if item["ai_confidence"] is not None else None
            ),
            threshold=threshold,
        )
        == bucket
    ]


def safe_agreement_bulk_approve(
    settings: Settings,
    *,
    actor: str,
    preview: bool,
    limit: int = 100,
    threshold: float = SAFE_APPROVAL_MIN_CONFIDENCE,
    vertical: str = "ALL",
) -> dict[str, Any]:
    """Preview/apply a bounded NurserySignal v1 backlog batch with QA holdout."""
    bounded_limit = min(max(int(limit), 1), 100)
    requested_vertical = validate_vertical_filter(vertical)
    if threshold != SAFE_APPROVAL_MIN_CONFIDENCE:
        raise ValueError("safe-approval-v1 uses the fixed 0.95 threshold")
    if requested_vertical == "CHILDRENS_HOME":
        return {
            "preview": preview,
            "policy_version": SAFE_APPROVAL_POLICY_VERSION,
            "vertical": requested_vertical,
            "eligible_total": 0,
            "would_auto_approve": 0,
            "qa_holdouts": 0,
            "remaining_manual": 0,
            "execution_allowed": False,
            "reason": "safe approval is not enabled for CareProspect",
        }
    policy_vertical = "NURSERY"
    with connection(settings) as conn:
        pending = _review_triage_rows(conn, "PENDING", policy_vertical)
    evaluated = []
    for item in pending:
        outcome = safe_approval_policy_outcome(
            signal_id=str(item["id"]),
            vertical=item["vertical"],
            source_type=item["source_type"],
            review_status=item["review_status"],
            metadata=item["metadata"],
            extracted_facts=item["extracted_facts"],
            ai_status=item["ai_status"],
            ai_recommendation=item["ai_recommendation"],
            ai_confidence=(
                float(item["ai_confidence"]) if item["ai_confidence"] is not None else None
            ),
        )
        if outcome != "INELIGIBLE":
            evaluated.append((str(item["id"]), outcome))
    batch = evaluated[:bounded_limit]
    auto_ids = [signal_id for signal_id, outcome in batch if outcome == "AUTO_APPROVE"]
    holdout_ids = [signal_id for signal_id, outcome in batch if outcome == "QA_HOLDOUT"]
    if preview:
        return {
            "preview": True,
            "policy_version": SAFE_APPROVAL_POLICY_VERSION,
            "eligible_total": len(evaluated),
            "batch_count": len(batch),
            "would_auto_approve": len(auto_ids),
            "qa_holdouts": len(holdout_ids),
            "remaining_manual": len(pending) - len(evaluated),
            "limit": bounded_limit,
            "threshold": threshold,
            "execution_allowed": True,
            "vertical": policy_vertical,
        }
    results = [
        apply_safe_approval_policy(settings, signal_id, trigger_actor=actor)
        for signal_id, _ in batch
    ]
    updated_ids = [item["signal_id"] for item in results if item["updated"]]
    return {
        "preview": False,
        "policy_version": SAFE_APPROVAL_POLICY_VERSION,
        "threshold": threshold,
        "requested": len(batch),
        "updated": len(updated_ids),
        "auto_approved": sum(
            item["updated"] and item["outcome"] == "AUTO_APPROVE" for item in results
        ),
        "qa_holdouts": sum(item["updated"] and item["outcome"] == "QA_HOLDOUT" for item in results),
        "signal_ids": updated_ids,
        "vertical": policy_vertical,
        "actor": actor,
    }


def nursery_planning_loss_backlog(
    settings: Settings, *, actor: str, preview: bool, limit: int = 100
) -> dict[str, Any]:
    """Preview/apply only historically validated explicit nursery-loss rejections."""
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        pending = _review_triage_rows(conn, "PENDING", "NURSERY")
        reviewed = _review_triage_rows(conn, "APPROVED", "NURSERY") + _review_triage_rows(
            conn, "REJECTED", "NURSERY"
        )

    def outcome(item: dict[str, Any], status: str | None = None) -> str:
        return explicit_nursery_loss_policy_outcome(
            signal_id=str(item["id"]),
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status=status or str(item["review_status"]),
            title=item.get("title"),
        )

    eligible = [item for item in pending if outcome(item) != "INELIGIBLE"]
    historical = [
        item
        for item in reviewed
        if not str(item.get("reviewed_by") or "").startswith("system:")
        and outcome(item, "PENDING") != "INELIGIBLE"
    ]
    historical_approved = sum(item["review_status"] == "APPROVED" for item in historical)
    execution_allowed = len(historical) >= 10 and historical_approved == 0
    batch = eligible[:bounded_limit]
    auto = [item for item in batch if outcome(item) == "AUTO_REJECT"]
    holdouts = [item for item in batch if outcome(item) == "QA_HOLDOUT"]
    result = {
        "preview": bool(preview),
        "policy_version": NURSERY_PLANNING_LOSS_POLICY_VERSION,
        "vertical": "NURSERY",
        "source_type": "planning",
        "pending_planning_total": sum(item["source_type"] == "planning" for item in pending),
        "eligible_total": len(eligible),
        "batch_count": len(batch),
        "would_auto_reject": len(auto),
        "qa_holdouts": len(holdouts),
        "limit": bounded_limit,
        "historical_validation": {
            "human_reviewed": len(historical),
            "approved": historical_approved,
            "rejected": len(historical) - historical_approved,
            "precision": (len(historical) - historical_approved) / len(historical)
            if historical
            else None,
        },
        "execution_allowed": execution_allowed,
        "samples": [
            {"signal_id": str(item["id"]), "title": item.get("title"), "outcome": outcome(item)}
            for item in batch[:10]
        ],
    }
    if preview or not execution_allowed:
        return result

    updated = rejected = holdout_writes = errors = 0
    for item in batch:
        signal_id = str(item["id"])
        decision = outcome(item)
        marker = {
            "policy_version": NURSERY_PLANNING_LOSS_POLICY_VERSION,
            "outcome": decision,
            "qa_bucket": UUID(signal_id).int % NURSERY_PLANNING_LOSS_QA_MODULUS,
            "reason": (
                "Primary planning application explicitly changes nursery use to "
                "non-nursery residential use."
            ),
            "trigger": "admin_backlog",
        }
        try:
            with connection(settings) as conn:
                if decision == "QA_HOLDOUT":
                    row = conn.execute(
                        """
                        UPDATE signal_enrichments
                        SET extracted_facts = extracted_facts || %s, updated_at = now()
                        WHERE raw_signal_id = %s AND review_status = 'PENDING'
                          AND COALESCE(
                            extracted_facts->'nursery_planning_loss_review'->>'policy_version', ''
                          ) <> %s
                        RETURNING raw_signal_id
                        """,
                        (
                            Jsonb({"nursery_planning_loss_review": marker}),
                            signal_id,
                            NURSERY_PLANNING_LOSS_POLICY_VERSION,
                        ),
                    ).fetchone()
                    action = "NURSERY_PLANNING_LOSS_QA_HOLDOUT"
                else:
                    row = conn.execute(
                        """
                        UPDATE signal_enrichments
                        SET review_status = 'REJECTED', reviewed_by = %s, reviewed_at = now(),
                            extracted_facts = extracted_facts || %s, updated_at = now()
                        WHERE raw_signal_id = %s AND review_status = 'PENDING'
                        RETURNING raw_signal_id
                        """,
                        (
                            f"system:{NURSERY_PLANNING_LOSS_POLICY_VERSION}",
                            Jsonb({"nursery_planning_loss_review": marker}),
                            signal_id,
                        ),
                    ).fetchone()
                    action = "NURSERY_PLANNING_LOSS_AUTO_REJECT"
                if row:
                    conn.execute(
                        """
                        INSERT INTO admin_audit_events
                            (action, actor, target_type, target_count, details, vertical)
                        VALUES (%s, %s, 'signal', 1, %s, 'NURSERY')
                        """,
                        (action, actor, Jsonb({"signal_id": signal_id, **marker})),
                    )
                    conn.commit()
                    updated += 1
                    rejected += int(decision == "AUTO_REJECT")
                    holdout_writes += int(decision == "QA_HOLDOUT")
        except Exception:
            errors += 1
    return {
        **result,
        "preview": False,
        "updated": updated,
        "auto_rejected": rejected,
        "qa_holdouts_recorded": holdout_writes,
        "errors": errors,
        "actor": actor,
    }


def nursery_planning_arboriculture_backlog(
    settings: Settings, *, actor: str, preview: bool, limit: int = 100
) -> dict[str, Any]:
    """Preview/apply the narrow tree-work disagreement policy only."""
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        pending = _review_triage_rows(conn, "PENDING", "NURSERY")
        reviewed = _review_triage_rows(conn, "APPROVED", "NURSERY") + _review_triage_rows(
            conn, "REJECTED", "NURSERY"
        )

    def outcome(item: dict[str, Any], status: str | None = None) -> str:
        return nursery_arboriculture_disagreement_policy_outcome(
            signal_id=str(item["id"]),
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status=status or str(item["review_status"]),
            title=item.get("title"),
            extracted_facts=item.get("extracted_facts"),
            ai_status=item.get("ai_status"),
            ai_recommendation=item.get("ai_recommendation"),
            ai_confidence=(float(item["ai_confidence"]) if item.get("ai_confidence") else None),
        )

    eligible = [item for item in pending if outcome(item) != "INELIGIBLE"]
    historical = [
        item
        for item in reviewed
        if not str(item.get("reviewed_by") or "").startswith("system:")
        and outcome(item, "PENDING") != "INELIGIBLE"
    ]
    historical_approved = sum(item["review_status"] == "APPROVED" for item in historical)
    # Eight exact human examples plus a structural tree-work exclusion is a
    # deliberately small, auditable initial cohort; the QA holdout remains.
    execution_allowed = len(historical) >= 8 and historical_approved == 0
    batch = eligible[:bounded_limit]
    auto = [item for item in batch if outcome(item) == "AUTO_REJECT"]
    holdouts = [item for item in batch if outcome(item) == "QA_HOLDOUT"]
    result = {
        "preview": bool(preview),
        "policy_version": NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION,
        "vertical": "NURSERY",
        "source_type": "planning",
        "pending_planning_total": sum(item["source_type"] == "planning" for item in pending),
        "eligible_total": len(eligible),
        "batch_count": len(batch),
        "would_auto_reject": len(auto),
        "qa_holdouts": len(holdouts),
        "limit": bounded_limit,
        "historical_validation": {
            "human_reviewed": len(historical),
            "approved": historical_approved,
            "rejected": len(historical) - historical_approved,
            "precision": (len(historical) - historical_approved) / len(historical)
            if historical
            else None,
        },
        "execution_allowed": execution_allowed,
        "samples": [
            {
                "signal_id": str(item["id"]),
                "title": item.get("title"),
                "ai_recommendation": item.get("ai_recommendation"),
                "ai_confidence": item.get("ai_confidence"),
                "outcome": outcome(item),
            }
            for item in batch[:10]
        ],
    }
    if preview or not execution_allowed:
        return result

    updated = rejected = holdout_writes = errors = 0
    for item in batch:
        signal_id = str(item["id"])
        decision = outcome(item)
        marker = {
            "policy_version": NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION,
            "outcome": decision,
            "qa_bucket": UUID(signal_id).int % NURSERY_PLANNING_ARBORICULTURE_QA_MODULUS,
            "reason": "Primary evidence is standalone arboricultural work, not childcare change.",
            "deterministic_recommendation": deterministic_review_recommendation(
                item.get("extracted_facts")
            ),
            "ai_recommendation": item.get("ai_recommendation"),
            # psycopg returns NUMERIC confidence as Decimal; audit JSON must
            # remain JSON-serialisable rather than failing the whole batch.
            "ai_confidence": (
                float(item["ai_confidence"]) if item.get("ai_confidence") is not None else None
            ),
            "trigger": "admin_backlog",
        }
        try:
            with connection(settings) as conn:
                if decision == "QA_HOLDOUT":
                    row = conn.execute(
                        """
                        UPDATE signal_enrichments
                        SET extracted_facts = extracted_facts || %s, updated_at = now()
                        WHERE raw_signal_id = %s AND review_status = 'PENDING'
                          AND COALESCE(extracted_facts->'nursery_planning_arboriculture_review'
                              ->>'policy_version', '') <> %s
                        RETURNING raw_signal_id
                        """,
                        (
                            Jsonb({"nursery_planning_arboriculture_review": marker}),
                            signal_id,
                            NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION,
                        ),
                    ).fetchone()
                    action = "NURSERY_PLANNING_ARBORICULTURE_QA_HOLDOUT"
                else:
                    row = conn.execute(
                        """
                        UPDATE signal_enrichments
                        SET review_status = 'REJECTED', reviewed_by = %s, reviewed_at = now(),
                            extracted_facts = extracted_facts || %s, updated_at = now()
                        WHERE raw_signal_id = %s AND review_status = 'PENDING'
                        RETURNING raw_signal_id
                        """,
                        (
                            f"system:{NURSERY_PLANNING_ARBORICULTURE_POLICY_VERSION}",
                            Jsonb({"nursery_planning_arboriculture_review": marker}),
                            signal_id,
                        ),
                    ).fetchone()
                    action = "NURSERY_PLANNING_ARBORICULTURE_AUTO_REJECT"
                if row:
                    conn.execute(
                        """
                        INSERT INTO admin_audit_events
                            (action, actor, target_type, target_count, details, vertical)
                        VALUES (%s, %s, 'signal', 1, %s, 'NURSERY')
                        """,
                        (action, actor, Jsonb({"signal_id": signal_id, **marker})),
                    )
                    conn.commit()
                    updated += 1
                    rejected += int(decision == "AUTO_REJECT")
                    holdout_writes += int(decision == "QA_HOLDOUT")
        except Exception:
            errors += 1
    return {
        **result,
        "preview": False,
        "updated": updated,
        "auto_rejected": rejected,
        "qa_holdouts_recorded": holdout_writes,
        "errors": errors,
        "actor": actor,
    }


def _routine_recruitment_review_rows(conn: Any, review_status: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rs.id, rs.vertical, rs.source_type, se.review_status, se.extracted_facts,
               se.reviewed_by, se.reviewed_at
        FROM raw_signals rs
        JOIN signal_enrichments se ON se.raw_signal_id = rs.id
        WHERE rs.vertical = 'NURSERY' AND rs.source_type = 'recruitment'
          AND se.review_status = %s
        ORDER BY rs.discovered_at DESC, rs.id DESC
        LIMIT 5000
        """,
        (review_status,),
    ).fetchall()
    fields = (
        "id",
        "vertical",
        "source_type",
        "review_status",
        "extracted_facts",
        "reviewed_by",
        "reviewed_at",
    )
    return [dict(zip(fields, row)) for row in rows]


def nursery_routine_recruitment_backlog(
    settings: Settings, *, actor: str, preview: bool, limit: int = 100
) -> dict[str, Any]:
    """Preview or apply the bounded, deterministic routine-recruitment policy.

    Historical validation is computed from human decisions only.  The policy is
    deliberately disabled unless that comparable cohort has at least 20 rows
    and no human rejections; this prevents an empty or noisy history becoming
    an automation authority.
    """
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        pending = _routine_recruitment_review_rows(conn, "PENDING")
        reviewed = _routine_recruitment_review_rows(
            conn, "APPROVED"
        ) + _routine_recruitment_review_rows(conn, "REJECTED")
    eligible = [
        item
        for item in pending
        if routine_recruitment_policy_outcome(
            signal_id=str(item["id"]),
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status=str(item["review_status"]),
            extracted_facts=item["extracted_facts"],
        )
        != "INELIGIBLE"
    ]
    historical = [
        item
        for item in reviewed
        if not str(item.get("reviewed_by") or "").startswith("system:")
        and routine_recruitment_policy_outcome(
            signal_id=str(item["id"]),
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status="PENDING",
            extracted_facts=item["extracted_facts"],
        )
        != "INELIGIBLE"
    ]
    historical_rejected = sum(item["review_status"] == "REJECTED" for item in historical)
    execution_allowed = len(historical) >= 20 and historical_rejected == 0
    batch = eligible[:bounded_limit]
    auto = [
        item
        for item in batch
        if routine_recruitment_policy_outcome(
            signal_id=str(item["id"]),
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status=str(item["review_status"]),
            extracted_facts=item["extracted_facts"],
        )
        == "AUTO_APPROVE"
    ]
    holdouts = [item for item in batch if item not in auto]
    result = {
        "preview": bool(preview),
        "policy_version": ROUTINE_RECRUITMENT_POLICY_VERSION,
        "vertical": "NURSERY",
        "source_type": "recruitment",
        "pending_recruitment_total": len(pending),
        "eligible_total": len(eligible),
        "batch_count": len(batch),
        "would_auto_approve": len(auto),
        "qa_holdouts": len(holdouts),
        "remaining_manual": len(pending) - len(eligible),
        "limit": bounded_limit,
        "historical_validation": {
            "human_reviewed": len(historical),
            "approved": len(historical) - historical_rejected,
            "rejected": historical_rejected,
            "precision": (len(historical) - historical_rejected) / len(historical)
            if historical
            else None,
        },
        "execution_allowed": execution_allowed,
        "samples": [
            {
                "signal_id": str(item["id"]),
                "outcome": routine_recruitment_policy_outcome(
                    signal_id=str(item["id"]),
                    vertical=str(item["vertical"]),
                    source_type=str(item["source_type"]),
                    review_status=str(item["review_status"]),
                    extracted_facts=item["extracted_facts"],
                ),
            }
            for item in batch[:10]
        ],
    }
    if preview or not execution_allowed:
        return result

    updated_ids: list[str] = []
    errors = 0
    for item in batch:
        signal_id = str(item["id"])
        outcome = routine_recruitment_policy_outcome(
            signal_id=signal_id,
            vertical=str(item["vertical"]),
            source_type=str(item["source_type"]),
            review_status=str(item["review_status"]),
            extracted_facts=item["extracted_facts"],
        )
        marker = {
            "policy_version": ROUTINE_RECRUITMENT_POLICY_VERSION,
            "outcome": outcome,
            "qa_bucket": routine_recruitment_qa_bucket(signal_id),
            "reason": "Routine Nursery recruitment supports an existing opportunity only.",
            "trigger": "admin_backlog",
        }
        try:
            with connection(settings) as conn:
                if outcome == "QA_HOLDOUT":
                    updated = conn.execute(
                        """
                        UPDATE signal_enrichments
                        SET extracted_facts = extracted_facts || %s, updated_at = now()
                        WHERE raw_signal_id = %s AND review_status = 'PENDING'
                          AND COALESCE(
                              extracted_facts->'routine_recruitment_review'->>'policy_version', ''
                          ) <> %s
                        RETURNING raw_signal_id
                        """,
                        (
                            Jsonb({"routine_recruitment_review": marker}),
                            signal_id,
                            ROUTINE_RECRUITMENT_POLICY_VERSION,
                        ),
                    ).fetchone()
                    action = "NURSERY_ROUTINE_RECRUITMENT_QA_HOLDOUT"
                else:
                    updated = conn.execute(
                        """UPDATE signal_enrichments SET review_status = 'APPROVED',
                               reviewed_by = %s, reviewed_at = now(),
                               extracted_facts = extracted_facts || %s, updated_at = now()
                               WHERE raw_signal_id = %s AND review_status = 'PENDING'
                               RETURNING raw_signal_id""",
                        (
                            f"system:{ROUTINE_RECRUITMENT_POLICY_VERSION}",
                            Jsonb({"routine_recruitment_review": marker}),
                            signal_id,
                        ),
                    ).fetchone()
                    action = "NURSERY_ROUTINE_RECRUITMENT_AUTO_APPROVE"
                if updated:
                    conn.execute(
                        """
                        INSERT INTO admin_audit_events
                            (action, actor, target_type, target_count, details, vertical)
                        VALUES (%s, %s, 'signal', 1, %s, 'NURSERY')
                        """,
                        (action, actor, Jsonb({"signal_id": signal_id, **marker})),
                    )
                    conn.commit()
                    updated_ids.append(signal_id)
        except Exception:
            errors += 1
    return {
        **result,
        "preview": False,
        "updated": len(updated_ids),
        "auto_approved": sum(str(item["id"]) in updated_ids for item in auto),
        "qa_holdouts_recorded": sum(str(item["id"]) in updated_ids for item in holdouts),
        "errors": errors,
        "signal_ids": updated_ids,
        "actor": actor,
    }


def recruitment_planning_diagnostic(settings: Settings, *, limit: int = 40) -> dict[str, Any]:
    """Explain, without linking, why relevant Care recruitment remains unmatched."""
    bounded_limit = min(max(int(limit), 1), 50)
    with connection(settings) as conn:
        recruitment_rows = conn.execute(
            """
            SELECT rs.id, rs.title, rs.raw_text, rs.location_hint, rs.organisation_hint,
                   rs.metadata, rs.discovered_at, se.extracted_facts
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'recruitment'
              AND se.review_status <> 'REJECTED'
              AND COALESCE(se.extracted_facts->>'recruitment_relevance', '')
                  IN ('RELEVANT_CHANGE', 'RELEVANT_ROUTINE')
              AND NOT EXISTS (
                  SELECT 1 FROM opportunity_signals os
                  WHERE os.raw_signal_id = rs.id AND os.status = 'ACTIVE'
              )
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT %s
            """,
            (bounded_limit,),
        ).fetchall()
        planning_rows = conn.execute(
            """
            SELECT rs.id, rs.title, rs.location_hint, rs.organisation_hint,
                   rs.metadata, rs.discovered_at, se.extracted_facts, rs.source_url
            FROM raw_signals rs
            JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
              AND se.review_status <> 'REJECTED'
            ORDER BY rs.discovered_at DESC, rs.id DESC
            LIMIT 2500
            """
        ).fetchall()

    recruitment_fields = (
        "id",
        "title",
        "raw_text",
        "location_hint",
        "organisation_hint",
        "metadata",
        "discovered_at",
        "extracted_facts",
    )
    planning_fields = (
        "id",
        "title",
        "location_hint",
        "organisation_hint",
        "metadata",
        "discovered_at",
        "extracted_facts",
        "source_url",
    )
    recruitment = [dict(zip(recruitment_fields, row)) for row in recruitment_rows]
    planning = [dict(zip(planning_fields, row)) for row in planning_rows]

    def postcode(item: dict[str, Any]) -> str:
        metadata = item.get("metadata") or {}
        value = metadata.get("postcode") or ""
        if not value:
            match = re.search(
                r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b",
                str(item.get("location_hint") or "").upper(),
            )
            value = match.group(0) if match else ""
        return str(value).upper().replace(" ", "")

    def locality(item: dict[str, Any]) -> str:
        metadata = item.get("metadata") or {}
        authority = metadata.get("authority")
        authority_name = authority.get("name") if isinstance(authority, dict) else None
        return normalize_identity(
            metadata.get("locality")
            or metadata.get("town")
            or metadata.get("council")
            or authority_name
        )

    results = []
    counts: dict[str, int] = {}
    for job in recruitment:
        job_postcode = postcode(job)
        job_outward = job_postcode[:-3] if len(job_postcode) > 3 else job_postcode
        job_locality = locality(job)
        job_name = job.get("organisation_hint") or job.get("title")
        candidates = []
        for plan in planning:
            plan_postcode = postcode(plan)
            plan_outward = plan_postcode[:-3] if len(plan_postcode) > 3 else plan_postcode
            plan_locality = locality(plan)
            plan_name = (
                plan.get("organisation_hint")
                or (plan.get("metadata") or {}).get("applicant")
                or plan.get("title")
            )
            exact_postcode = bool(job_postcode and plan_postcode == job_postcode)
            outward_match = bool(job_outward and plan_outward == job_outward)
            locality_match = bool(job_locality and plan_locality == job_locality)
            name_match = compatible_names(job_name, plan_name)
            if (
                exact_postcode
                or (outward_match and (locality_match or name_match))
                or (locality_match and name_match)
            ):
                candidates.append(
                    {
                        **plan,
                        "postcode": plan_postcode,
                        "exact_postcode": exact_postcode,
                        "outward_match": outward_match,
                        "locality_match": locality_match,
                        "name_match": name_match,
                        "candidate_name": plan_name,
                    }
                )
        exact = [item for item in candidates if item["exact_postcode"]]
        stale = [
            item
            for item in candidates
            if any(
                term
                in normalize_identity(
                    f"{item.get('title')} {(item.get('metadata') or {}).get('procedure')}"
                )
                for term in (
                    "non material amendment",
                    "condition discharge",
                    "discharge of condition",
                )
            )
        ]
        if not candidates:
            category = (
                "POSTCODE_MISSING_OR_MISMATCHED"
                if not job_postcode and job_locality
                else "NO_RELEVANT_PLANNING_FOUND"
            )
        elif stale and len(stale) == len(candidates):
            category = "PLANNING_EXISTS_BUT_NON_MATERIAL/STALE"
        elif len(candidates) > 1 and not exact:
            category = "MULTIPLE_POSSIBLE_SITES"
        elif exact and any(item["name_match"] for item in exact):
            category = "SAME_PROJECT_CLEAR_TO_HUMAN_BUT_MATCHER_MISSED"
        elif exact:
            plan_applicant = (exact[0].get("metadata") or {}).get("applicant")
            category = (
                "APPLICANT_IS_DIFFERENT_ENTITY"
                if plan_applicant and job.get("organisation_hint")
                else "OPERATOR_NAME_MISMATCH"
            )
        elif not job_postcode:
            category = "LOCATION_TOO_COARSE"
        elif candidates:
            category = "POSSIBLE_PROJECT_BUT_IDENTITY_TOO_WEAK"
        else:
            category = "OTHER"
        counts[category] = counts.get(category, 0) + 1
        results.append(
            {
                "recruitment_signal_id": str(job["id"]),
                "title": job["title"],
                "operator": job.get("organisation_hint"),
                "location": job.get("location_hint"),
                "postcode": job_postcode or None,
                "relevance": (job.get("extracted_facts") or {}).get("recruitment_relevance"),
                "diagnostic_outcome": category,
                "candidate_count": len(candidates),
                "planning_candidates": [
                    {
                        "signal_id": str(item["id"]),
                        "title": item["title"],
                        "location": item.get("location_hint"),
                        "postcode": item["postcode"] or None,
                        "applicant": (item.get("metadata") or {}).get("applicant"),
                        "exact_postcode": item["exact_postcode"],
                        "locality_match": item["locality_match"],
                        "name_match": item["name_match"],
                        "source_url": item.get("source_url"),
                    }
                    for item in candidates[:5]
                ],
            }
        )
    return {
        "sample_size": len(results),
        "requested_limit": bounded_limit,
        "category_counts": counts,
        "items": results,
        "read_only": True,
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


def _planning_family_row(conn: Any, identity: PlanningFamilyIdentity, vertical: str) -> Any:
    return conn.execute(
        """INSERT INTO planning_application_families
              (vertical, planning_authority, normalized_authority, raw_reference,
               normalized_reference)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (vertical, normalized_authority, normalized_reference)
           DO UPDATE SET planning_authority = EXCLUDED.planning_authority,
                         raw_reference = EXCLUDED.raw_reference, updated_at = now()
           RETURNING id, primary_signal_id, origin_status""",
        (
            vertical,
            identity.authority,
            identity.normalized_authority,
            identity.raw_reference,
            identity.normalized_reference,
        ),
    ).fetchone()


def _attach_family_support(conn: Any, family_id: Any, origin_signal_id: Any) -> dict[str, int]:
    counts = {"followups_linked": 0, "origins_linked": 0, "conflicts": 0}
    origin_opportunities = conn.execute(
        """SELECT opportunity_id FROM opportunity_signals
           WHERE raw_signal_id = %s AND status = 'ACTIVE' ORDER BY created_at""",
        (origin_signal_id,),
    ).fetchall()
    followups = conn.execute(
        """SELECT r.raw_signal_id, se.extracted_facts, rs.metadata
           FROM planning_signal_family_relationships r
           JOIN raw_signals rs ON rs.id = r.raw_signal_id
           LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
           WHERE r.family_id = %s AND r.relationship_type = 'REFERENCES_APPLICATION'""",
        (family_id,),
    ).fetchall()
    if len(origin_opportunities) > 1:
        counts["conflicts"] += 1
        return counts
    origin_opportunity = origin_opportunities[0][0] if origin_opportunities else None
    for followup_id, facts, metadata in followups:
        facts = facts if isinstance(facts, dict) else {}
        if not followup_can_support_opportunity(facts.get("planning_subtype"), metadata):
            continue
        existing = conn.execute(
            """SELECT opportunity_id, status FROM opportunity_signals
               WHERE raw_signal_id = %s ORDER BY created_at""",
            (followup_id,),
        ).fetchall()
        active = [row[0] for row in existing if row[1] == "ACTIVE"]
        if origin_opportunity and active and origin_opportunity not in active:
            counts["conflicts"] += 1
            continue
        if origin_opportunity and not active:
            blocked = any(row[0] == origin_opportunity and row[1] == "REJECTED" for row in existing)
            if blocked:
                continue
            result = conn.execute(
                """INSERT INTO opportunity_signals
                     (opportunity_id, raw_signal_id, vertical, relationship_type,
                      extracted_facts, provenance, status, created_by, match_outcome,
                      match_confidence, match_reason)
                   VALUES (%s, %s, 'CHILDRENS_HOME', 'SUPPORTS', %s, %s,
                           'ACTIVE', 'SYSTEM', 'STRONG', 1.0,
                           'exact authority-scoped planning-family reference')
                   ON CONFLICT (opportunity_id, raw_signal_id) DO NOTHING
                   RETURNING raw_signal_id""",
                (
                    origin_opportunity,
                    followup_id,
                    Jsonb(facts),
                    Jsonb(
                        {
                            "method": PLANNING_FAMILY_POLICY_VERSION,
                            "family_id": str(family_id),
                            "relationship": "SUPPORT_EXISTING_ONLY",
                        }
                    ),
                ),
            ).fetchone()
            counts["followups_linked"] += int(result is not None)
        elif not origin_opportunity and len(active) == 1:
            blocked = conn.execute(
                """SELECT 1 FROM opportunity_signals WHERE opportunity_id = %s
                   AND raw_signal_id = %s AND status = 'REJECTED'""",
                (active[0], origin_signal_id),
            ).fetchone()
            if blocked:
                continue
            result = conn.execute(
                """INSERT INTO opportunity_signals
                     (opportunity_id, raw_signal_id, vertical, relationship_type,
                      extracted_facts, provenance, status, created_by, match_outcome,
                      match_confidence, match_reason)
                   SELECT %s, rs.id, rs.vertical, 'SUPPORTS', se.extracted_facts, %s,
                          'ACTIVE', 'SYSTEM', 'STRONG', 1.0,
                          'foundational planning-family origin'
                   FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   WHERE rs.id = %s AND se.review_status = 'APPROVED'
                   ON CONFLICT (opportunity_id, raw_signal_id) DO NOTHING
                   RETURNING raw_signal_id""",
                (
                    active[0],
                    Jsonb({"method": PLANNING_FAMILY_POLICY_VERSION, "family_id": str(family_id)}),
                    origin_signal_id,
                ),
            ).fetchone()
            counts["origins_linked"] += int(result is not None)
    return counts


def reconcile_planning_family_signal(settings: Settings, signal_id: str) -> dict[str, Any]:
    """Resolve one Care Planning signal by exact authority-scoped references."""
    result = {
        "families_created_or_reused": 0,
        "origins_resolved": 0,
        "followups_linked": 0,
        "origins_linked": 0,
        "conflicts": 0,
        "missing_origins": 0,
    }
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT rs.id, rs.external_id, rs.title, rs.raw_text, rs.metadata, rs.vertical,
                      se.extracted_facts
               FROM raw_signals rs LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE rs.id = %s AND rs.source_type = 'planning'
                 AND rs.vertical = 'CHILDRENS_HOME'""",
            (signal_id,),
        ).fetchone()
        if not row:
            return result
        raw = {
            "id": row[0],
            "external_id": row[1],
            "title": row[2],
            "raw_text": row[3],
            "metadata": row[4] or {},
            "vertical": row[5],
        }
        facts = row[6] if isinstance(row[6], dict) else {}
        authority = planning_authority(raw["metadata"])
        normalized_authority = normalize_planning_authority(authority)
        if not authority or not normalized_authority:
            return result
        primary_reference = primary_planning_reference(raw["external_id"], raw["metadata"])
        conn.execute(
            """UPDATE raw_signals SET planning_authority_normalized = %s,
                       planning_reference_normalized = %s WHERE id = %s""",
            (normalized_authority, primary_reference, signal_id),
        )
        if primary_reference:
            identity = PlanningFamilyIdentity.create(authority, primary_reference)
            family = _planning_family_row(conn, identity, row[5]) if identity else None
            if family:
                result["families_created_or_reused"] += 1
                relationship = "PRIMARY_APPLICATION"
                conn.execute(
                    """INSERT INTO planning_signal_family_relationships
                         (family_id, raw_signal_id, relationship_type, provenance)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (
                        family[0],
                        signal_id,
                        relationship,
                        Jsonb(
                            {
                                "method": PLANNING_FAMILY_POLICY_VERSION,
                                "raw_reference": primary_reference,
                            }
                        ),
                    ),
                )
                conn.execute(
                    """UPDATE planning_origin_recovery_attempts
                       SET recovered_signal_id = %s
                       WHERE family_id = %s AND status = 'FOUND'
                         AND recovered_signal_id IS NULL""",
                    (signal_id, family[0]),
                )
                if is_foundational_planning_signal(facts.get("planning_subtype"), raw["metadata"]):
                    conn.execute(
                        """UPDATE planning_application_families SET primary_signal_id = %s,
                                  origin_status = 'RESOLVED', updated_at = now() WHERE id = %s""",
                        (signal_id, family[0]),
                    )
                    linked = _attach_family_support(conn, family[0], signal_id)
                    result["origins_resolved"] += 1
                    for key in ("followups_linked", "origins_linked", "conflicts"):
                        result[key] += linked[key]
                elif canonical_planning_outcome(raw["metadata"]).outcome in {
                    PlanningOutcome.REFUSED,
                    PlanningOutcome.WITHDRAWN,
                    PlanningOutcome.APPEAL_DISMISSED,
                }:
                    conn.execute(
                        """UPDATE planning_application_families SET primary_signal_id = %s,
                                  origin_status = 'NEGATIVE', updated_at = now() WHERE id = %s""",
                        (signal_id, family[0]),
                    )
        for raw_reference, normalized_reference in prior_planning_references(raw, facts):
            identity = PlanningFamilyIdentity.create(authority, raw_reference)
            if identity is None or normalized_reference == primary_reference:
                continue
            family = _planning_family_row(conn, identity, row[5])
            result["families_created_or_reused"] += 1
            conn.execute(
                """INSERT INTO planning_signal_family_relationships
                     (family_id, raw_signal_id, relationship_type, provenance)
                   VALUES (%s, %s, 'REFERENCES_APPLICATION', %s)
                   ON CONFLICT DO NOTHING""",
                (
                    family[0],
                    signal_id,
                    Jsonb(
                        {
                            "method": PLANNING_FAMILY_POLICY_VERSION,
                            "raw_reference": raw_reference,
                        }
                    ),
                ),
            )
            candidates = conn.execute(
                """SELECT rs.id, rs.metadata, se.extracted_facts
                   FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
                     AND rs.planning_authority_normalized = %s
                     AND rs.planning_reference_normalized = %s AND rs.id <> %s""",
                (identity.normalized_authority, identity.normalized_reference, signal_id),
            ).fetchall()
            foundations = [
                candidate
                for candidate in candidates
                if is_foundational_planning_signal(
                    (candidate[2] or {}).get("planning_subtype"), candidate[1]
                )
            ]
            negative_origins = [
                candidate
                for candidate in candidates
                if canonical_planning_outcome(candidate[1]).outcome
                in {
                    PlanningOutcome.REFUSED,
                    PlanningOutcome.WITHDRAWN,
                    PlanningOutcome.APPEAL_DISMISSED,
                }
            ]
            if len(foundations) == 1:
                origin_id = foundations[0][0]
                conn.execute(
                    """UPDATE planning_application_families SET primary_signal_id = %s,
                              origin_status = 'RESOLVED', updated_at = now() WHERE id = %s""",
                    (origin_id, family[0]),
                )
                conn.execute(
                    """INSERT INTO planning_signal_family_relationships
                         (family_id, raw_signal_id, relationship_type, provenance)
                       VALUES (%s, %s, 'PRIMARY_APPLICATION', %s)
                       ON CONFLICT DO NOTHING""",
                    (family[0], origin_id, Jsonb({"method": PLANNING_FAMILY_POLICY_VERSION})),
                )
                linked = _attach_family_support(conn, family[0], origin_id)
                result["origins_resolved"] += 1
                for key in ("followups_linked", "origins_linked", "conflicts"):
                    result[key] += linked[key]
            elif len(foundations) > 1 or len(negative_origins) > 1:
                conn.execute(
                    """UPDATE planning_application_families
                       SET origin_status = 'AMBIGUOUS', updated_at = now()
                       WHERE id = %s""",
                    (family[0],),
                )
                result["conflicts"] += 1
            elif len(negative_origins) == 1:
                origin_id = negative_origins[0][0]
                conn.execute(
                    """UPDATE planning_application_families SET primary_signal_id = %s,
                              origin_status = 'NEGATIVE', updated_at = now() WHERE id = %s""",
                    (origin_id, family[0]),
                )
                conn.execute(
                    """INSERT INTO planning_signal_family_relationships
                         (family_id, raw_signal_id, relationship_type, provenance)
                       VALUES (%s, %s, 'PRIMARY_APPLICATION', %s)
                       ON CONFLICT DO NOTHING""",
                    (family[0], origin_id, Jsonb({"method": PLANNING_FAMILY_POLICY_VERSION})),
                )
            else:
                result["missing_origins"] += 1
        conn.commit()
    return result


def planning_family_historical_preview(settings: Settings, *, limit: int = 2500) -> dict[str, Any]:
    """Read-only inventory of exact-reference Care Planning origin resolution."""
    bounded_limit = min(max(int(limit), 1), 5000)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT rs.id, rs.external_id, rs.title, rs.raw_text, rs.metadata,
                      se.extracted_facts,
                      EXISTS (SELECT 1 FROM opportunity_signals os
                              WHERE os.raw_signal_id = rs.id AND os.status = 'ACTIVE')
               FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
               ORDER BY rs.created_at, rs.id LIMIT %s""",
            (bounded_limit,),
        ).fetchall()
    primaries: dict[tuple[str, str], list[dict[str, Any]]] = {}
    followups: list[dict[str, Any]] = []
    for row in rows:
        raw = {
            "id": str(row[0]),
            "external_id": row[1],
            "title": row[2],
            "raw_text": row[3],
            "metadata": row[4] or {},
        }
        authority = planning_authority(raw["metadata"])
        normalized_authority = normalize_planning_authority(authority)
        primary = primary_planning_reference(raw["external_id"], raw["metadata"])
        facts = row[5] if isinstance(row[5], dict) else {}
        item = {**raw, "authority": authority, "facts": facts, "has_opportunity": bool(row[6])}
        if normalized_authority and primary:
            primaries.setdefault((normalized_authority, primary), []).append(item)
        refs = prior_planning_references(raw, facts)
        if refs:
            item["references"] = refs
            followups.append(item)
    counts = {
        "followup_signals_with_prior_references": len(followups),
        "stored_origin_found": 0,
        "stored_negative_origin": 0,
        "no_stored_origin": 0,
        "multiple_candidate_origins": 0,
        "no_usable_authority_or_reference": 0,
        "followup_only_opportunities": 0,
        "could_gain_foundational_support_without_plota": 0,
        "requires_targeted_plota_lookup": 0,
    }
    examples: dict[str, list[dict[str, Any]]] = {
        "stored_origin_found": [],
        "no_stored_origin": [],
        "stored_negative_origin": [],
        "multiple_candidate_origins": [],
        "no_usable_authority_or_reference": [],
    }
    for item in followups:
        authority_key = normalize_planning_authority(item["authority"])
        origin_signal_ids: list[str] = []
        if not authority_key:
            counts["no_usable_authority_or_reference"] += 1
            category = "no_usable_authority_or_reference"
        else:
            candidates: dict[str, dict[str, Any]] = {}
            for _, normalized_reference in item["references"]:
                for candidate in primaries.get((authority_key, normalized_reference), []):
                    if candidate["id"] != item["id"]:
                        candidates[candidate["id"]] = candidate
            foundations = [
                candidate
                for candidate in candidates.values()
                if is_foundational_planning_signal(
                    candidate["facts"].get("planning_subtype"), candidate["metadata"]
                )
            ]
            negatives = [
                candidate
                for candidate in candidates.values()
                if canonical_planning_outcome(candidate["metadata"]).outcome
                in {
                    PlanningOutcome.REFUSED,
                    PlanningOutcome.WITHDRAWN,
                    PlanningOutcome.APPEAL_DISMISSED,
                }
            ]
            if len(foundations) == 1:
                category = "stored_origin_found"
                counts[category] += 1
                origin_signal_ids = [foundations[0]["id"]]
                if item["has_opportunity"] and not foundations[0]["has_opportunity"]:
                    counts["could_gain_foundational_support_without_plota"] += 1
            elif len(foundations) > 1 or len(negatives) > 1:
                category = "multiple_candidate_origins"
                counts[category] += 1
            elif len(negatives) == 1:
                category = "stored_negative_origin"
                counts[category] += 1
            else:
                category = "no_stored_origin"
                counts[category] += 1
                counts["requires_targeted_plota_lookup"] += 1
        if item["has_opportunity"] and category != "stored_origin_found":
            counts["followup_only_opportunities"] += 1
        if len(examples[category]) < 5:
            examples[category].append(
                {
                    "signal_id": item["id"],
                    "external_id": item["external_id"],
                    "authority": item["authority"],
                    "references": [value[0] for value in item["references"]],
                    "title": str(item["title"] or "")[:240],
                    "origin_signal_ids": origin_signal_ids,
                }
            )
    return {
        "preview": True,
        "policy_version": PLANNING_FAMILY_POLICY_VERSION,
        "planning_signals_inspected": len(rows),
        **counts,
        "examples": examples,
        "external_provider_requests": 0,
        "read_only": True,
    }


def backfill_historical_planning_family_metadata(
    settings: Settings,
    *,
    actor: str,
    preview: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Create local family provenance for historical Care Planning follow-ups only."""
    bounded_limit = min(max(int(limit), 1), 100)
    bounded_offset = max(int(offset), 0)
    subtype_values = sorted(SUPPORT_ONLY_SUBTYPES)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT rs.id, rs.external_id, rs.title, rs.raw_text, rs.metadata,
                      se.extracted_facts
               FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
                 AND se.extracted_facts->>'planning_subtype' = ANY(%s)
               ORDER BY rs.created_at, rs.id LIMIT %s OFFSET %s""",
            (subtype_values, bounded_limit, bounded_offset),
        ).fetchall()
        candidate_rows = conn.execute(
            """SELECT rs.id, rs.external_id, rs.metadata, se.extracted_facts
               FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'"""
        ).fetchall()

        primaries: dict[tuple[str, str], list[tuple[Any, dict[str, Any], dict[str, Any]]]] = {}
        for candidate_id, external_id, metadata, facts in candidate_rows:
            metadata = metadata if isinstance(metadata, dict) else {}
            authority_key = normalize_planning_authority(planning_authority(metadata))
            reference = primary_planning_reference(external_id, metadata)
            if authority_key and reference:
                primaries.setdefault((authority_key, reference), []).append(
                    (
                        candidate_id,
                        metadata,
                        facts if isinstance(facts, dict) else {},
                    )
                )

        result = {
            "preview": bool(preview),
            "policy_version": PLANNING_FAMILY_HISTORICAL_BACKFILL_VERSION,
            "signals_inspected": len(rows),
            "usable_prior_reference_signals": 0,
            "already_family_linked": 0,
            "missing_family_metadata": 0,
            "exact_stored_origins_available": 0,
            "families_created": 0,
            "families_reused": 0,
            "references_relationships_created": 0,
            "origins_marked_missing": 0,
            "stored_origins_resolved_locally": 0,
            "ambiguous_or_conflicting": 0,
            "missing_authority_or_reference": 0,
            "multiple_reference_signals": 0,
            "skipped": 0,
            "failures": 0,
            "plota_requests": 0,
            "plota_records_consumed": 0,
            "external_provider_calls": 0,
            "limit": bounded_limit,
            "offset": bounded_offset,
        }
        seen_existing_signals: set[str] = set()
        seen_missing_signals: set[str] = set()
        counted_origin_keys: set[tuple[str, str]] = set()
        counted_family_keys: set[tuple[str, str]] = set()
        counted_missing_keys: set[tuple[str, str]] = set()

        for signal_id, external_id, title, raw_text, metadata, facts in rows:
            metadata = metadata if isinstance(metadata, dict) else {}
            facts = facts if isinstance(facts, dict) else {}
            raw = {
                "id": signal_id,
                "external_id": external_id,
                "title": title,
                "raw_text": raw_text,
                "metadata": metadata,
            }
            authority = planning_authority(metadata)
            authority_key = normalize_planning_authority(authority)
            references = prior_planning_references(raw, facts)
            if not authority_key or not references:
                result["missing_authority_or_reference"] += 1
                result["skipped"] += 1
                continue
            result["usable_prior_reference_signals"] += 1
            if len(references) > 1:
                result["multiple_reference_signals"] += 1
            signal_has_existing = True
            signal_has_missing = False
            for raw_reference, normalized_reference in references:
                identity = PlanningFamilyIdentity.create(authority, raw_reference)
                if identity is None:
                    result["missing_authority_or_reference"] += 1
                    result["skipped"] += 1
                    continue
                key = (identity.normalized_authority, identity.normalized_reference)
                candidates = [
                    candidate for candidate in primaries.get(key, []) if candidate[0] != signal_id
                ]
                foundations = [
                    candidate
                    for candidate in candidates
                    if is_foundational_planning_signal(
                        candidate[2].get("planning_subtype"), candidate[1]
                    )
                ]
                negatives = [
                    candidate
                    for candidate in candidates
                    if canonical_planning_outcome(candidate[1]).outcome
                    in {
                        PlanningOutcome.REFUSED,
                        PlanningOutcome.WITHDRAWN,
                        PlanningOutcome.APPEAL_DISMISSED,
                    }
                ]
                if len(foundations) > 1 or len(negatives) > 1:
                    result["ambiguous_or_conflicting"] += 1
                    result["skipped"] += 1
                    continue
                if (foundations or negatives) and key not in counted_origin_keys:
                    result["exact_stored_origins_available"] += 1
                    counted_origin_keys.add(key)

                family = conn.execute(
                    """SELECT id, origin_status FROM planning_application_families
                       WHERE vertical = 'CHILDRENS_HOME' AND normalized_authority = %s
                         AND normalized_reference = %s""",
                    key,
                ).fetchone()
                relationship = None
                if family:
                    relationship = conn.execute(
                        """SELECT 1 FROM planning_signal_family_relationships
                           WHERE family_id = %s AND raw_signal_id = %s
                             AND relationship_type = 'REFERENCES_APPLICATION'""",
                        (family[0], signal_id),
                    ).fetchone()
                if relationship:
                    continue
                signal_has_existing = False
                signal_has_missing = True
                if not family and key not in counted_family_keys:
                    result["families_created"] += 1
                    counted_family_keys.add(key)
                elif family and key not in counted_family_keys:
                    result["families_reused"] += 1
                    counted_family_keys.add(key)
                result["references_relationships_created"] += 1
                if not foundations and not negatives and key not in counted_missing_keys:
                    result["origins_marked_missing"] += 1
                    counted_missing_keys.add(key)
                if foundations and key not in counted_missing_keys:
                    result["stored_origins_resolved_locally"] += 1
                    counted_missing_keys.add(key)

                if preview:
                    continue
                try:
                    family = family or _planning_family_row(conn, identity, "CHILDRENS_HOME")
                    conn.execute(
                        """INSERT INTO planning_signal_family_relationships
                             (family_id, raw_signal_id, relationship_type, provenance)
                           VALUES (%s, %s, 'REFERENCES_APPLICATION', %s)
                           ON CONFLICT DO NOTHING""",
                        (
                            family[0],
                            signal_id,
                            Jsonb(
                                {
                                    "method": PLANNING_FAMILY_HISTORICAL_BACKFILL_VERSION,
                                    "raw_reference": raw_reference,
                                    "backfilled_at": datetime.now(UTC).isoformat(),
                                }
                            ),
                        ),
                    )
                    origin = foundations[0] if foundations else negatives[0] if negatives else None
                    if origin:
                        origin_status = "RESOLVED" if foundations else "NEGATIVE"
                        conn.execute(
                            """UPDATE planning_application_families
                               SET primary_signal_id = %s, origin_status = %s, updated_at = now()
                               WHERE id = %s""",
                            (origin[0], origin_status, family[0]),
                        )
                        conn.execute(
                            """INSERT INTO planning_signal_family_relationships
                                 (family_id, raw_signal_id, relationship_type, provenance)
                               VALUES (%s, %s, 'PRIMARY_APPLICATION', %s)
                               ON CONFLICT DO NOTHING""",
                            (
                                family[0],
                                origin[0],
                                Jsonb({"method": PLANNING_FAMILY_HISTORICAL_BACKFILL_VERSION}),
                            ),
                        )
                except Exception:
                    result["failures"] += 1
                    raise
            signal_key = str(signal_id)
            if signal_has_existing and signal_key not in seen_existing_signals:
                result["already_family_linked"] += 1
                seen_existing_signals.add(signal_key)
            if signal_has_missing and signal_key not in seen_missing_signals:
                result["missing_family_metadata"] += 1
                seen_missing_signals.add(signal_key)

        if preview:
            conn.rollback()
        else:
            conn.commit()

    mutation_count = result["references_relationships_created"] - result["failures"]
    if not preview and mutation_count > 0:
        record_admin_audit(
            settings,
            action="PLANNING_FAMILY_HISTORICAL_METADATA_BACKFILL",
            actor=actor,
            target_type="planning_application_family",
            details={**result, "external_requests": 0, "vertical": "CHILDRENS_HOME"},
        )
    return result


def reconcile_stored_planning_families(
    settings: Settings,
    *,
    actor: str,
    limit: int = 2500,
    signal_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Bounded, idempotent stored-evidence reconciliation; never calls Plota."""
    bounded_limit = min(max(int(limit), 1), 5000)
    explicit_selection = signal_ids is not None
    if signal_ids is not None and len(signal_ids) > 100:
        raise ValueError("signal_ids must contain at most 100 IDs")
    requested_ids = [str(UUID(value)) for value in (signal_ids or [])]
    with connection(settings) as conn:
        if explicit_selection:
            if not requested_ids:
                rows = []
            else:
                rows = conn.execute(
                    """SELECT rs.id, se.extracted_facts
                       FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                       WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
                         AND rs.id = ANY(%s::uuid[])
                       ORDER BY CASE WHEN se.extracted_facts->>'planning_subtype' IN
                         ('NEW_HOME_CHANGE_OF_USE','NEW_HOME_OTHER_EXPLICIT',
                          'NEW_HOME_MIXED_USE','LAWFULNESS_PROPOSED',
                          'EXPANSION_OR_CAPACITY_CHANGE') THEN 0 ELSE 1 END,
                         rs.created_at, rs.id LIMIT %s""",
                    (requested_ids, min(bounded_limit, len(requested_ids))),
                ).fetchall()
        else:
            rows = conn.execute(
                """SELECT rs.id, se.extracted_facts
                   FROM raw_signals rs JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   WHERE rs.vertical = 'CHILDRENS_HOME' AND rs.source_type = 'planning'
                   ORDER BY CASE WHEN se.extracted_facts->>'planning_subtype' IN
                     ('NEW_HOME_CHANGE_OF_USE','NEW_HOME_OTHER_EXPLICIT','NEW_HOME_MIXED_USE',
                      'LAWFULNESS_PROPOSED','EXPANSION_OR_CAPACITY_CHANGE') THEN 0 ELSE 1 END,
                     rs.created_at, rs.id LIMIT %s""",
                (bounded_limit,),
            ).fetchall()
    totals = {
        "signals_inspected": 0,
        "families_created_or_reused": 0,
        "origins_resolved": 0,
        "followups_linked": 0,
        "origins_linked": 0,
        "conflicts": 0,
        "missing_origins": 0,
        "errors": 0,
    }
    for signal_id, _ in rows:
        try:
            outcome = reconcile_planning_family_signal(settings, str(signal_id))
            totals["signals_inspected"] += 1
            for key in outcome:
                totals[key] += int(outcome[key])
        except Exception:
            totals["errors"] += 1
    record_admin_audit(
        settings,
        action="PLANNING_FAMILY_STORED_RECONCILIATION",
        actor=actor,
        target_type="planning_application_family",
        details={
            **totals,
            "target_count": totals["signals_inspected"],
            "policy_version": PLANNING_FAMILY_POLICY_VERSION,
            "external_requests": 0,
            "vertical": "CHILDRENS_HOME",
            "explicit_signal_selection": explicit_selection,
        },
    )
    return {
        **totals,
        "policy_version": PLANNING_FAMILY_POLICY_VERSION,
        "external_requests": 0,
        "explicit_signal_selection": explicit_selection,
    }


def queue_planning_origin_recovery(
    settings: Settings,
    *,
    signal_id: str | None = None,
    actor: str,
    limit: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    """Queue bounded exact-reference recovery for unresolved families."""
    if not settings.planning_manual_run_queue_url:
        raise RuntimeError("PLANNING_MANUAL_RUN_QUEUE_URL is not configured")
    bounded_limit = min(max(int(limit), 1), 25)
    signal_clause = "AND r.raw_signal_id = %s" if signal_id else ""
    params: list[Any] = [signal_id] if signal_id else []
    params.append(bounded_limit)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT f.id, f.planning_authority, f.normalized_reference,
                               r.raw_signal_id, attempt.status, attempt.retry_after,
                               COALESCE(opportunity.address, rs.location_hint) AS site_address,
                               COALESCE(
                                 opportunity.postcode, rs.metadata->>'postcode'
                               ) AS site_postcode,
                               opportunity.town, rs.external_id, rs.metadata
                FROM planning_application_families f
                JOIN planning_signal_family_relationships r ON r.family_id = f.id
                JOIN raw_signals rs ON rs.id = r.raw_signal_id
                LEFT JOIN LATERAL (
                  SELECT status, retry_after FROM planning_origin_recovery_attempts
                  WHERE family_id = f.id ORDER BY attempted_at DESC, id DESC LIMIT 1
                ) attempt ON TRUE
                LEFT JOIN LATERAL (
                  SELECT o.address, o.postcode, o.town
                  FROM opportunity_signals os JOIN opportunities o ON o.id = os.opportunity_id
                  WHERE os.raw_signal_id = r.raw_signal_id AND os.status = 'ACTIVE'
                  ORDER BY o.created_at, o.id LIMIT 1
                ) opportunity ON TRUE
                WHERE f.vertical = 'CHILDRENS_HOME'
                  AND f.origin_status IN ('MISSING', 'AMBIGUOUS')
                  AND r.relationship_type = 'REFERENCES_APPLICATION'
                  {signal_clause}
                  AND (%s OR attempt.status IS NULL
                       OR attempt.status = 'PROVIDER_ERROR'
                       OR (attempt.status = 'NOT_FOUND' AND attempt.retry_after <= now()))
                ORDER BY f.id LIMIT %s""",
            [*params[:-1], force, params[-1]],
        ).fetchall()
        queued: list[dict[str, Any]] = []
        for (
            family_id,
            authority,
            reference,
            trigger_id,
            _,
            _,
            address,
            postcode,
            town,
            external_id,
            metadata,
        ) in rows:
            source_metadata = metadata if isinstance(metadata, dict) else {}
            provider_record = source_metadata.get("provider_record")
            provider_record = provider_record if isinstance(provider_record, dict) else {}
            triggering_provider_id = (
                source_metadata.get("provider_application_id")
                or provider_record.get("id")
                or str(external_id or "").removeprefix("plota:")
            )
            attempt = conn.execute(
                """INSERT INTO planning_origin_recovery_attempts
                     (family_id, triggering_signal_id, status, details)
                   VALUES (%s, %s, 'QUEUED', %s) RETURNING id""",
                (
                    family_id,
                    trigger_id,
                    Jsonb(
                        {
                            "reason": "REFERENCED_APPLICATION_MISSING",
                            "actor": actor,
                            "policy_version": PLANNING_FAMILY_POLICY_VERSION,
                            "provider_query": reference,
                        }
                    ),
                ),
            ).fetchone()
            queued.append(
                {
                    "attempt_id": str(attempt[0]),
                    "family_id": str(family_id),
                    "triggering_signal_id": str(trigger_id),
                    "planning_authority": authority,
                    "normalized_reference": reference,
                    "site_address": address,
                    "site_postcode": postcode,
                    "site_town": town,
                    "triggering_provider_id": triggering_provider_id,
                }
            )
        conn.commit()
    sent = 0
    for item in queued:
        try:
            response = boto3.client("sqs").send_message(
                QueueUrl=settings.planning_manual_run_queue_url,
                MessageBody=json.dumps(
                    {"invocation_source": "planning_origin_recovery", **item},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            if response.get("MessageId"):
                sent += 1
        except Exception:
            with connection(settings) as conn:
                conn.execute(
                    """UPDATE planning_origin_recovery_attempts SET status = 'PROVIDER_ERROR',
                              retry_after = now() + INTERVAL '1 hour',
                              details = details || %s WHERE id = %s""",
                    (Jsonb({"queue_error": True}), item["attempt_id"]),
                )
                conn.commit()
            raise
    return {"selected": len(queued), "queued": sent, "limit": bounded_limit}


def update_planning_origin_recovery(
    settings: Settings,
    *,
    attempt_id: str,
    status: str,
    recovered_signal_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if status not in {"RUNNING", "FOUND", "NOT_FOUND", "AMBIGUOUS", "PROVIDER_ERROR"}:
        raise ValueError("invalid planning origin recovery status")
    with connection(settings) as conn:
        conn.execute(
            """UPDATE planning_origin_recovery_attempts
               SET status = %s, recovered_signal_id = %s,
                   retry_after = CASE WHEN %s = 'NOT_FOUND' THEN now() + INTERVAL '30 days'
                                      WHEN %s = 'PROVIDER_ERROR' THEN now() + INTERVAL '1 hour'
                                      ELSE NULL END,
                   details = details || %s, attempted_at = now()
               WHERE id = %s""",
            (status, recovered_signal_id, status, status, Jsonb(details or {}), attempt_id),
        )
        conn.execute(
            """UPDATE planning_application_families f
               SET origin_status = CASE WHEN %s = 'AMBIGUOUS' THEN 'AMBIGUOUS'
                                        ELSE 'MISSING' END,
                   updated_at = now()
               FROM planning_origin_recovery_attempts attempt
               WHERE attempt.id = %s AND attempt.family_id = f.id
                 AND f.origin_status IN ('MISSING', 'AMBIGUOUS')
                 AND %s IN ('NOT_FOUND', 'AMBIGUOUS', 'PROVIDER_ERROR')""",
            (status, attempt_id, status),
        )
        conn.commit()


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
                 ORDER BY o.updated_at DESC""",
            (signal_vertical,),
        ).fetchall()
        match = None
        match_reason = None
        uncertain_matches: list[tuple[Any, float, str]] = []
        if source_type == "planning":
            authority = normalize_planning_authority(planning_authority(metadata))
            reference = primary_planning_reference("", metadata)
            if authority and reference:
                family_matches = conn.execute(
                    """SELECT DISTINCT o.id
                       FROM planning_application_families f
                       JOIN planning_signal_family_relationships family_signal
                         ON family_signal.family_id = f.id
                       JOIN opportunity_signals os
                         ON os.raw_signal_id = family_signal.raw_signal_id
                        AND os.status = 'ACTIVE'
                       JOIN opportunities o ON o.id = os.opportunity_id
                       WHERE f.vertical = %s
                         AND f.normalized_authority = %s
                         AND f.normalized_reference = %s
                         AND o.review_status NOT IN ('MERGED', 'REJECTED')
                       ORDER BY o.id
                       LIMIT 2""",
                    (signal_vertical, authority, reference),
                ).fetchall()
                if len(family_matches) == 1:
                    family_opportunity_id = family_matches[0][0]
                    blocked = conn.execute(
                        """SELECT 1 FROM opportunity_signals
                           WHERE opportunity_id = %s AND raw_signal_id = %s
                             AND status = 'REJECTED'""",
                        (family_opportunity_id, signal_id),
                    ).fetchone()
                    if not blocked:
                        match = next(
                            (row for row in existing if row[0] == family_opportunity_id), None
                        )
                        match_reason = "exact authority-scoped planning-family reference"
                elif len(family_matches) > 1:
                    for family_opportunity_id, *_ in family_matches:
                        uncertain_matches.append(
                            (
                                family_opportunity_id,
                                0.9,
                                "planning family is linked to multiple opportunities",
                            )
                        )
        prior_references = [
            normalized
            for value in (candidate.get("extracted_facts") or {}).get(
                "planning_prior_references", []
            )
            if (normalized := normalize_planning_reference(value))
        ]
        if source_type == "planning" and prior_references and match is None:
            authority = normalize_planning_authority(planning_authority(metadata))
            referenced = conn.execute(
                """
                SELECT o.id
                FROM opportunities o
                JOIN opportunity_signals os ON os.opportunity_id = o.id
                JOIN raw_signals prior ON prior.id = os.raw_signal_id
                WHERE o.vertical = %s AND o.review_status NOT IN ('MERGED', 'REJECTED')
                  AND os.status = 'ACTIVE' AND prior.source_type = 'planning'
                  AND prior.planning_authority_normalized = %s
                  AND prior.planning_reference_normalized = ANY(%s)
                ORDER BY o.updated_at DESC
                LIMIT 2
                """,
                (
                    signal_vertical,
                    authority,
                    prior_references,
                ),
            ).fetchall()
            if len(referenced) == 1:
                referenced_id = referenced[0][0]
                blocked = conn.execute(
                    """SELECT 1 FROM opportunity_signals
                       WHERE opportunity_id = %s AND raw_signal_id = %s
                         AND status = 'REJECTED'""",
                    (referenced_id, signal_id),
                ).fetchone()
                if not blocked:
                    match = next((row for row in existing if row[0] == referenced_id), None)
                    match_reason = "references an earlier linked planning application"
            elif len(referenced) > 1:
                for referenced_id, *_ in referenced:
                    uncertain_matches.append(
                        (
                            referenced_id,
                            0.7,
                            "planning reference points to multiple possible opportunities",
                        )
                    )
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
        elif match is None:
            for row in existing:
                comparison = classify_match(postcode, name, row[6], row[7] or row[1])
                operator_comparison = classify_match(postcode, operator, row[6], row[7])
                if comparison.outcome == "UNCERTAIN" or operator_comparison.outcome == "UNCERTAIN":
                    uncertain = (
                        comparison if comparison.outcome == "UNCERTAIN" else operator_comparison
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
    settings: Settings,
    *,
    limit: int,
    offset: int,
    search: str | None = None,
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
            "OR COALESCE(o.stage_reason, '') ILIKE %s "
            "OR COALESCE(o.operator_name, '') ILIKE %s "
            "OR COALESCE(o.address, '') ILIKE %s "
            "OR COALESCE(o.postcode, '') ILIKE %s "
            "OR COALESCE(o.town, '') ILIKE %s)"
        )
        pattern = f"%{search[:200]}%"
        params.extend([pattern] * 7)
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
                       o.customer_lifecycle_stage, o.publication_status,
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
        "customer_lifecycle_stage",
        "publication_status",
        "signal_count",
    )
    return {
        "items": [dict(zip(fields, row)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _care_policy_opportunities(
    conn: Any, *, opportunity_id: str | None = None
) -> list[dict[str, Any]]:
    where = "WHERE o.vertical = 'CHILDRENS_HOME'"
    params: tuple[Any, ...] = ()
    if opportunity_id:
        where += " AND o.id = %s"
        params = (opportunity_id,)
    rows = conn.execute(
        f"""
            SELECT o.id, o.name, o.event_type, o.lifecycle_stage, o.confidence,
                   o.review_status, o.publication_status, o.vertical, o.operator_name,
                   o.address, o.postcode, o.town, o.merged_into_opportunity_id,
                   o.change_type, o.stage_reason, o.creation_reason,
                   o.customer_lifecycle_stage, o.publication_automation_blocked,
                   o.publication_automation_reason, o.customer_title, o.customer_summary,
                   o.customer_published_by, o.customer_published_at,
                   o.publication_automation_provenance, o.customer_withdrawn_at,
                   o.withdrawal_automation_provenance,
                   COALESCE(rel.relationships, '[]'::jsonb),
                   COALESCE(hist.actions, ARRAY[]::text[]),
                   COALESCE(audit.actions, ARRAY[]::text[]),
                   COALESCE(matches.pending_count, 0),
                   COALESCE(watches.enabled_count, 0)
            FROM opportunities o
            LEFT JOIN LATERAL (
              SELECT jsonb_agg(jsonb_build_object(
                'id', rs.id, 'signal_id', rs.id, 'status', os.status,
                'relationship_status', os.status, 'source_type', rs.source_type,
                'external_id', rs.external_id,
                'title', rs.title, 'metadata', rs.metadata,
                'discovered_at', rs.discovered_at,
                'latest_revision_at', (SELECT max(rev.observed_at)
                  FROM raw_signal_revisions rev WHERE rev.raw_signal_id = rs.id),
                'review_status', se.review_status, 'extracted_facts', se.extracted_facts,
                'relationship_extracted_facts', os.extracted_facts,
                'planning_family_relationship_types', COALESCE(
                  (SELECT jsonb_agg(DISTINCT pfr.relationship_type)
                   FROM planning_signal_family_relationships pfr
                   WHERE pfr.raw_signal_id = rs.id), '[]'::jsonb)
              ) ORDER BY rs.discovered_at, rs.id) AS relationships
              FROM opportunity_signals os
              JOIN raw_signals rs ON rs.id = os.raw_signal_id
              LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
              WHERE os.opportunity_id = o.id
            ) rel ON TRUE
            LEFT JOIN LATERAL (
              SELECT array_agg(DISTINCT h.action) AS actions
              FROM opportunity_signal_history h WHERE h.opportunity_id = o.id
            ) hist ON TRUE
            LEFT JOIN LATERAL (
              SELECT array_agg(DISTINCT a.action) AS actions
              FROM admin_audit_events a
              WHERE a.target_type IN ('opportunity', 'opportunity_match')
                AND (a.details->>'opportunity_id' = o.id::text
                  OR a.details->>'source_opportunity_id' = o.id::text
                  OR a.details->>'target_opportunity_id' = o.id::text
                  OR a.details->>'new_opportunity_id' = o.id::text)
            ) audit ON TRUE
            LEFT JOIN LATERAL (
              SELECT count(*) AS pending_count FROM opportunity_match_reviews mr
              WHERE mr.opportunity_id = o.id AND mr.status = 'PENDING'
            ) matches ON TRUE
            LEFT JOIN LATERAL (
              SELECT count(*) AS enabled_count FROM planning_lifecycle_watches watch
              WHERE watch.opportunity_id = o.id AND watch.enabled
            ) watches ON TRUE
            {where}
            ORDER BY o.id LIMIT 5000
            """,
        params,
    ).fetchall()
    fields = (
        "id",
        "name",
        "event_type",
        "lifecycle_stage",
        "confidence",
        "review_status",
        "publication_status",
        "vertical",
        "operator_name",
        "address",
        "postcode",
        "town",
        "merged_into_opportunity_id",
        "change_type",
        "stage_reason",
        "creation_reason",
        "customer_lifecycle_stage",
        "publication_automation_blocked",
        "publication_automation_reason",
        "customer_title",
        "customer_summary",
        "customer_published_by",
        "customer_published_at",
        "publication_automation_provenance",
        "customer_withdrawn_at",
        "withdrawal_automation_provenance",
        "relationships",
        "history_actions",
        "audit_actions",
        "pending_match_reviews",
        "enabled_planning_watches",
    )
    return [dict(zip(fields, row)) for row in rows]


def _care_publication_projection(
    opportunity: dict[str, Any], hygiene_item: dict[str, Any]
) -> tuple[dict[str, Any], str | None, str | None, Any]:
    projection = {
        **opportunity,
        "local_authority": next(
            (
                (signal.get("metadata") or {}).get("local_authority")
                or (signal.get("metadata") or {}).get("council")
                for signal in opportunity["relationships"] or []
                if signal.get("status") == "ACTIVE"
                and (
                    (signal.get("metadata") or {}).get("local_authority")
                    or (signal.get("metadata") or {}).get("council")
                )
            ),
            None,
        ),
        "region": next(
            (
                (signal.get("metadata") or {}).get("region")
                for signal in opportunity["relationships"] or []
                if (signal.get("metadata") or {}).get("region")
            ),
            None,
        ),
        "source_types": hygiene_item.get("source_types") or [],
    }
    generated_title = generated_customer_title(projection)
    generated_summary = generated_customer_summary(projection)
    safe_title = str(opportunity.get("customer_title") or generated_title or "").strip() or None
    safe_summary = (
        str(opportunity.get("customer_summary") or generated_summary or "").strip() or None
    )
    lifecycle = str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW")
    decision = evaluate_publication(
        projection,
        lifecycle,
        opportunity["relationships"] or [],
        hygiene_category=hygiene_item["category"],
        hygiene_warning=hygiene_item.get("warning"),
        safe_title=safe_title,
        safe_summary=safe_summary,
    )
    return projection, safe_title, safe_summary, decision


def care_opportunity_lifecycle_preview(settings: Settings) -> dict[str, Any]:
    """Derive Phase-A lifecycle/publication/withdrawal policy results without mutation."""
    with connection(settings) as conn:
        opportunities = _care_policy_opportunities(conn)
        watch_rows = conn.execute(
            """WITH watch AS (
                 SELECT count(*) AS total,
                        count(*) FILTER (WHERE enabled) AS enabled,
                        count(*) FILTER (
                          WHERE enabled AND next_eligible_refresh_at <= now()
                        ) AS due,
                        count(*) FILTER (
                          WHERE last_checked_at >= now() - interval '24 hours'
                        ) AS checked_24h,
                        count(*) FILTER (WHERE consecutive_provider_errors > 0) AS errors,
                        min(next_eligible_refresh_at) FILTER (WHERE enabled) AS next_poll,
                        jsonb_object_agg(cadence_days::text, cadence_count)
                          FILTER (WHERE cadence_days IS NOT NULL) AS cadence
                 FROM (
                   SELECT w.*,
                          count(*) OVER (PARTITION BY cadence_days) AS cadence_count
                   FROM planning_lifecycle_watches w
                 ) grouped
               ), runs AS (
                 SELECT COALESCE(sum(provider_requests) FILTER (
                          WHERE created_at >= date_trunc('day', now())
                        ), 0) AS requests_today,
                        COALESCE(sum(provider_requests) FILTER (
                          WHERE created_at >= date_trunc('month', now())
                        ), 0) AS requests_month,
                        count(*) FILTER (WHERE status = 'CHANGED') AS changed,
                        count(*) FILTER (WHERE status = 'UNCHANGED') AS unchanged,
                        count(*) FILTER (
                          WHERE status IN ('FAILED', 'RATE_LIMITED', 'QUEUE_FAILED')
                        ) AS failed
                 FROM planning_lifecycle_watch_runs
               )
               SELECT watch.total, watch.enabled, watch.due, watch.checked_24h,
                      watch.errors, watch.next_poll, watch.cadence,
                      state.execution_enabled, state.emergency_reason,
                      state.max_polls_per_execution,
                      state.max_provider_requests_per_day,
                      state.max_provider_requests_per_month,
                      runs.requests_today, runs.requests_month,
                      runs.changed, runs.unchanged, runs.failed,
                      (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                         'run_id', recent.id, 'watch_id', recent.watch_id,
                         'status', recent.status, 'error_category', recent.error_category,
                         'completed_at', recent.completed_at
                       ) ORDER BY recent.created_at DESC), '[]'::jsonb)
                       FROM (SELECT id, watch_id, status, error_category,
                                    completed_at, created_at
                             FROM planning_lifecycle_watch_runs
                             WHERE status IN ('FAILED', 'RATE_LIMITED', 'QUEUE_FAILED')
                             ORDER BY created_at DESC LIMIT 5) recent) AS recent_failures
               FROM watch CROSS JOIN runs
               CROSS JOIN planning_lifecycle_watcher_state state
               WHERE state.singleton"""
        ).fetchone()
        publication_state = conn.execute(
            """SELECT execution_enabled, recurring_enabled, emergency_reason,
                      max_publications_per_execution, policy_version,
                      last_execution_at, last_selected, last_published,
                      last_skipped, last_failed, updated_at,
                      (SELECT count(*) FROM opportunities
                       WHERE vertical = 'CHILDRENS_HOME'
                         AND publication_status = 'PUBLISHED'
                         AND publication_automation_provenance ? 'policy_version'),
                      (SELECT count(*) FROM opportunities
                       WHERE vertical = 'CHILDRENS_HOME'
                         AND publication_status = 'PUBLISHED'
                         AND NOT (publication_automation_provenance ? 'policy_version')),
                      (SELECT COALESCE(jsonb_agg(jsonb_build_object(
                         'run_id', r.id, 'opportunity_id', i.opportunity_id,
                         'reason', i.reason, 'created_at', i.created_at
                       ) ORDER BY i.created_at DESC), '[]'::jsonb)
                       FROM (SELECT * FROM care_publication_run_items
                             WHERE status = 'FAILED'
                             ORDER BY created_at DESC LIMIT 5) i
                       JOIN care_publication_runs r ON r.id = i.run_id)
               FROM care_publication_automation_state WHERE singleton""",
        ).fetchone()
    hygiene = audit_opportunities(opportunities)
    hygiene_by_id = {item["opportunity_id"]: item for item in hygiene["items"]}
    lifecycle_counts: Counter[str] = Counter()
    stored_lifecycle_counts: Counter[str] = Counter()
    publication_counts: Counter[str] = Counter()
    publication_exclusions: Counter[str] = Counter()
    publication_by_lifecycle: dict[str, Counter[str]] = {}
    publication_by_source_mix: dict[str, Counter[str]] = {}
    publication_samples: dict[str, list[dict[str, Any]]] = {}
    previous_publication_counts: Counter[str] = Counter()
    legacy_publication_counts: Counter[str] = Counter()
    publication_changed_examples: list[dict[str, Any]] = []
    publication_change_transitions: Counter[str] = Counter()
    publication_changed_count = 0
    published_policy_conflicts: list[dict[str, Any]] = []
    conflict_primary_reasons: Counter[str] = Counter()
    conflict_lifecycles: Counter[str] = Counter()
    conflict_source_mix: Counter[str] = Counter()
    conflict_change_types: Counter[str] = Counter()
    conflict_planning_subtypes: Counter[str] = Counter()
    conflict_judgements: Counter[str] = Counter()
    conflict_customer_useful: Counter[str] = Counter()
    conflict_recommendations: Counter[str] = Counter()
    conflict_samples: dict[str, list[dict[str, Any]]] = {}
    manual_review_cohorts: Counter[str] = Counter()
    manual_review_samples: dict[str, list[dict[str, Any]]] = {}
    withdrawal_counts: Counter[str] = Counter()
    published_lifecycle_counts: Counter[str] = Counter()
    watch_outcomes: Counter[str] = Counter()
    watch_eligibility: Counter[str] = Counter()
    watch_exclusions: Counter[str] = Counter()
    watch_cadences: Counter[str] = Counter()
    watch_eligible_lifecycles: Counter[str] = Counter()
    watch_excluded_lifecycles: Counter[str] = Counter()
    watch_monthly_by_lifecycle: Counter[str] = Counter()
    watch_samples: dict[str, list[dict[str, Any]]] = {}
    watched_candidates: set[str] = set()
    watch_candidates_evaluated = 0
    further_relaxation_candidates = 0
    examples: list[dict[str, Any]] = []

    for opportunity in opportunities:
        opportunity_id = str(opportunity["id"])
        decision = derive_care_lifecycle(opportunity["relationships"] or [])
        lifecycle_counts[decision.lifecycle.value] += 1
        stored_lifecycle_counts[str(opportunity.get("customer_lifecycle_stage") or "UNSET")] += 1
        item = hygiene_by_id[opportunity_id]
        stored_lifecycle = str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW")
        projection, safe_title, safe_summary, publication = _care_publication_projection(
            opportunity, item
        )
        projection["derived_customer_lifecycle"] = decision.lifecycle.value
        publication_counts[publication.outcome] += 1
        publication_exclusions.update(publication.exclusions)
        publication_by_lifecycle.setdefault(publication.outcome, Counter())[stored_lifecycle] += 1
        source_mix = str(item.get("source_mix") or "NONE")
        publication_by_source_mix.setdefault(publication.outcome, Counter())[source_mix] += 1
        samples = publication_samples.setdefault(publication.outcome, [])
        if len(samples) < 5:
            samples.append(
                {
                    "opportunity_id": opportunity_id,
                    "lifecycle": stored_lifecycle,
                    "change_type": opportunity.get("change_type"),
                    "source_mix": source_mix,
                    "hygiene_category": item["category"],
                    "reason": publication.reason,
                    "reasons": list(publication.exclusions),
                    "publication_status": opportunity.get("publication_status"),
                }
            )

        previous = evaluate_publication_v2(
            projection,
            stored_lifecycle,
            opportunity["relationships"] or [],
            hygiene_category=item["category"],
            hygiene_warning=item.get("warning"),
            safe_title=safe_title,
            safe_summary=safe_summary,
        )
        previous_publication_counts[previous.outcome] += 1
        legacy = evaluate_publication_v1(
            projection,
            decision,
            opportunity["relationships"] or [],
            hygiene_category=item["category"],
            hygiene_warning=item.get("warning"),
            safe_title=safe_title,
            safe_summary=safe_summary,
        )
        legacy_publication_counts[legacy.outcome] += 1
        if previous.outcome != publication.outcome:
            publication_changed_count += 1
            publication_change_transitions[f"{previous.outcome}->{publication.outcome}"] += 1
            if len(publication_changed_examples) < 10:
                publication_changed_examples.append(
                    {
                        "opportunity_id": opportunity_id,
                        "previous_outcome": previous.outcome,
                        "current_outcome": publication.outcome,
                        "primary_reason": publication.reason,
                        "policy_rule": (
                            "legacy_strong_opening_compatibility"
                            if "legacy_strong_opening_compatibility" in publication.exclusions
                            else "unchanged_v2_rule"
                        ),
                        "lifecycle": stored_lifecycle,
                        "supporting_evidence_summary": (
                            f"{item.get('foundational_signal_count') or 0} foundational signal(s); "
                            f"source mix {source_mix}."
                        ),
                        "current_reasons": list(publication.exclusions),
                    }
                )

        if previous.outcome == "MANUAL_REVIEW":
            if stored_lifecycle == "APPEAL_PENDING":
                cohort = "unresolved_appeal"
            elif stored_lifecycle == "NEEDS_REVIEW":
                cohort = "needs_review_lifecycle"
            elif (
                opportunity.get("change_type") == "EXPANSION"
                and stored_lifecycle == "PLANNING_APPROVED"
            ):
                cohort = "approved_expansion"
            elif (
                opportunity.get("change_type") == "EXPANSION"
                and stored_lifecycle == "PLANNING_PENDING"
            ):
                cohort = "pending_expansion"
            elif stored_lifecycle == "PLANNING_PENDING" and legacy_strong_opening_signals(
                projection, opportunity["relationships"] or []
            ):
                cohort = "strong_pending_opening_pre_taxonomy"
            elif stored_lifecycle == "PLANNING_APPROVED":
                cohort = "approved_other"
            elif stored_lifecycle == "PLANNING_PENDING":
                cohort = "pending_other"
            else:
                cohort = "other"
            manual_review_cohorts[cohort] += 1
            cohort_samples = manual_review_samples.setdefault(cohort, [])
            if len(cohort_samples) < 5:
                planning_subtypes = sorted(
                    {
                        str(
                            (signal.get("extracted_facts") or {}).get("planning_subtype") or "UNSET"
                        )
                        for signal in opportunity["relationships"] or []
                        if signal.get("source_type") == "planning"
                        and signal.get("status") == "ACTIVE"
                    }
                )
                cohort_samples.append(
                    {
                        "opportunity_id": opportunity_id,
                        "lifecycle": stored_lifecycle,
                        "change_type": opportunity.get("change_type"),
                        "planning_subtypes": planning_subtypes,
                        "v2_reasons": list(previous.exclusions),
                        "v3_outcome": publication.outcome,
                    }
                )

        if opportunity.get("publication_status") == "PUBLISHED":
            underlying_hygiene_category = item["category"]
            if (
                underlying_hygiene_category == "MANUAL_OR_ADMIN_TOUCHED_PRESERVE"
                and set(item.get("admin_touch_types") or []) <= {"manual_publication"}
                and int(item.get("foundational_signal_count") or 0) > 0
            ):
                underlying_hygiene_category = "VALID_SUPPORTED"
            v2_underlying = evaluate_publication_v2(
                projection,
                stored_lifecycle,
                opportunity["relationships"] or [],
                hygiene_category=underlying_hygiene_category,
                hygiene_warning=item.get("warning"),
                safe_title=safe_title,
                safe_summary=safe_summary,
                respect_existing_publication=False,
            )
            if v2_underlying.outcome not in {"AUTO_PUBLISH_ELIGIBLE", "QA_HOLDOUT"}:
                assessment = classify_publication_conflict(
                    projection,
                    stored_lifecycle,
                    opportunity["relationships"] or [],
                    hygiene_category=underlying_hygiene_category,
                    v2_decision=v2_underlying,
                )
                conflict = {
                    "opportunity_id": opportunity_id,
                    "lifecycle": stored_lifecycle,
                    "source_mix": source_mix,
                    "change_type": opportunity.get("change_type"),
                    "planning_subtypes": list(assessment.planning_subtypes),
                    "primary_reason": assessment.primary_reason,
                    "judgement": assessment.judgement,
                    "customer_useful": assessment.customer_useful,
                    "recommendation": assessment.recommendation,
                    "why_manually_published": (
                        "An explicit human publication decision predates or overrides "
                        "the automatic scope."
                    ),
                    "v2_outcome_without_protection": v2_underlying.outcome,
                    "v2_reasons": list(v2_underlying.exclusions),
                    "evidence_summary": assessment.evidence_summary,
                }
                published_policy_conflicts.append(conflict)
                conflict_primary_reasons[assessment.primary_reason] += 1
                conflict_lifecycles[stored_lifecycle] += 1
                conflict_source_mix[source_mix] += 1
                conflict_change_types[str(opportunity.get("change_type") or "UNKNOWN")] += 1
                for subtype in assessment.planning_subtypes:
                    conflict_planning_subtypes[subtype] += 1
                conflict_judgements[assessment.judgement] += 1
                conflict_customer_useful[str(assessment.customer_useful).lower()] += 1
                conflict_recommendations[assessment.recommendation] += 1
                conflict_samples.setdefault(assessment.primary_reason, []).append(conflict)
        withdrawal = evaluate_withdrawal(
            projection,
            stored_lifecycle,
            opportunity["relationships"] or [],
            hygiene_category=item["category"],
            hygiene_warning=item.get("warning"),
        )
        withdrawal_counts[withdrawal.outcome] += 1
        if opportunity.get("publication_status") == "PUBLISHED":
            published_lifecycle_counts[decision.lifecycle.value] += 1

        planning_signals = [
            signal
            for signal in opportunity["relationships"] or []
            if signal.get("source_type") == "planning" and signal.get("status") == "ACTIVE"
        ]
        if not planning_signals:
            watch_exclusions["no_relevant_planning_evidence"] += 1
            watch_excluded_lifecycles[
                str(opportunity.get("customer_lifecycle_stage") or "UNSET")
            ] += 1
            samples = watch_samples.setdefault("no_relevant_planning_evidence", [])
            if len(samples) < 3:
                samples.append(
                    {
                        "opportunity_id": opportunity_id,
                        "signal_id": None,
                        "planning_reference": None,
                        "lifecycle": str(opportunity.get("customer_lifecycle_stage") or "UNSET"),
                        "planning_outcome": None,
                        "age_days": None,
                        "age_source": None,
                        "cadence_days": None,
                    }
                )
        for signal in planning_signals:
            authority = planning_authority(signal.get("metadata") or {})
            reference = primary_planning_reference(
                signal.get("external_id"), signal.get("metadata") or {}
            )
            watch = evaluate_planning_watch(
                opportunity,
                signal,
                has_planning_identity=bool(authority and reference),
            )
            watch_candidates_evaluated += 1
            lifecycle_value = str(opportunity.get("customer_lifecycle_stage") or "UNSET")
            sample = {
                "opportunity_id": opportunity_id,
                "signal_id": str(signal.get("id") or ""),
                "planning_reference": reference,
                "lifecycle": lifecycle_value,
                "planning_outcome": watch.planning_outcome,
                "age_days": watch.age_days,
                "age_source": watch.age_source,
                "cadence_days": watch.cadence_days,
            }
            samples = watch_samples.setdefault(watch.reason, [])
            if len(samples) < 3:
                samples.append(sample)
            if not watch.eligible:
                watch_exclusions[watch.reason] += 1
                watch_excluded_lifecycles[lifecycle_value] += 1
                continue
            watched_candidates.add(str(signal["id"]))
            watch_eligibility[watch.reason] += 1
            watch_outcomes[watch.planning_outcome] += 1
            watch_eligible_lifecycles[lifecycle_value] += 1
            watch_cadences[f"{watch.cadence_days}_days"] += 1
            monthly_requests = 30 / int(watch.cadence_days or 30)
            watch_monthly_by_lifecycle[lifecycle_value] += monthly_requests
            if watch.cadence_days == 30 and watch.age_days is not None and watch.age_days > 365:
                further_relaxation_candidates += 1
        if len(examples) < 25 and (
            publication.outcome != "NOT_ELIGIBLE"
            or withdrawal.outcome in {"AUTO_WITHDRAW", "MANUAL_REVIEW"}
            or decision.lifecycle == CareLifecycle.NEEDS_REVIEW
        ):
            examples.append(
                {
                    "opportunity_id": opportunity_id,
                    "name": opportunity.get("name"),
                    "derived_lifecycle": decision.lifecycle.value,
                    "lifecycle_reason": decision.reason,
                    "publication_outcome": publication.outcome,
                    "publication_exclusions": list(publication.exclusions),
                    "withdrawal_outcome": withdrawal.outcome,
                    "publication_status": opportunity.get("publication_status"),
                }
            )

    estimated_daily, estimated_monthly = project_planning_watch_requests(watch_cadences)
    previous_monthly = CARE_PLANNING_WATCHER_PREVIOUS_MONTHLY_REQUESTS
    reduction = round((previous_monthly - estimated_monthly) / previous_monthly * 100, 1)
    return {
        "preview": True,
        "mutations": 0,
        "policy_versions": {
            "lifecycle": CARE_LIFECYCLE_POLICY_VERSION,
            "publication": CARE_PUBLICATION_POLICY_VERSION,
            "withdrawal": CARE_WITHDRAWAL_POLICY_VERSION,
            "planning_watcher": CARE_PLANNING_WATCHER_POLICY_VERSION,
        },
        "opportunities_inspected": len(opportunities),
        "inventory_complete": len(opportunities) < 5000,
        "derived_lifecycle_counts": dict(sorted(lifecycle_counts.items())),
        "stored_lifecycle_counts": dict(sorted(stored_lifecycle_counts.items())),
        "watcher": {
            "status": (
                "ENABLED"
                if bool(watch_rows[7]) and settings.care_lifecycle_watcher_schedule_enabled
                else "DISABLED"
            ),
            "execution_enabled": bool(watch_rows[7]),
            "schedule_enabled": settings.care_lifecycle_watcher_schedule_enabled,
            "emergency_reason": watch_rows[8],
            "policy_version": CARE_PLANNING_WATCHER_POLICY_VERSION,
            "opportunities_evaluated": len(opportunities),
            "watch_candidates_evaluated": watch_candidates_evaluated,
            "would_watch": len(watched_candidates),
            "eligible_watches": len(watched_candidates),
            "excluded_watches": sum(watch_exclusions.values()),
            "eligibility_reasons": dict(sorted(watch_eligibility.items())),
            "exclusion_reasons": dict(sorted(watch_exclusions.items())),
            "eligible_by_lifecycle": dict(sorted(watch_eligible_lifecycles.items())),
            "excluded_by_lifecycle": dict(sorted(watch_excluded_lifecycles.items())),
            "cadence_counts": dict(sorted(watch_cadences.items())),
            "candidate_outcomes": dict(sorted(watch_outcomes.items())),
            "stored_total": int(watch_rows[0] or 0),
            "stored_enabled": int(watch_rows[1] or 0),
            "due": int(watch_rows[2] or 0),
            "checked_last_24h": int(watch_rows[3] or 0),
            "provider_errors": int(watch_rows[4] or 0),
            "next_poll_at": watch_rows[5],
            "persisted_cadence_counts": {
                f"{key}_days": int(value) for key, value in (watch_rows[6] or {}).items()
            },
            "estimated_requests_per_day": estimated_daily,
            "estimated_requests_per_30_days": estimated_monthly,
            "previous_estimated_requests_per_30_days": previous_monthly,
            "estimated_monthly_reduction": round(previous_monthly - estimated_monthly, 1),
            "estimated_reduction_percent": reduction,
            "projected_requests_by_lifecycle_per_30_days": {
                key: round(value, 1) for key, value in sorted(watch_monthly_by_lifecycle.items())
            },
            "further_relaxation_candidates": further_relaxation_candidates,
            "samples": watch_samples,
            "provider_requests_executed": int(watch_rows[13] or 0),
            "provider_requests_today": int(watch_rows[12] or 0),
            "provider_requests_this_month": int(watch_rows[13] or 0),
            "changed_polls": int(watch_rows[14] or 0),
            "unchanged_polls": int(watch_rows[15] or 0),
            "failed_polls": int(watch_rows[16] or 0),
            "recent_failures": watch_rows[17] or [],
            "quota_guardrails": {
                "max_polls_per_execution": int(watch_rows[9] or 0),
                "max_provider_requests_per_day": int(watch_rows[10] or 0),
                "max_provider_requests_per_month": int(watch_rows[11] or 0),
                "daily_reached": int(watch_rows[12] or 0) >= int(watch_rows[10] or 0),
                "monthly_reached": int(watch_rows[13] or 0) >= int(watch_rows[11] or 0),
            },
            "watch_enrolments": int(watch_rows[0] or 0),
            "lifecycle_changes": 0,
            "publication_changes": 0,
            "withdrawal_changes": 0,
        },
        "publication_preview": {
            "preview_only": True,
            "policy_version": CARE_PUBLICATION_POLICY_VERSION,
            "previous_policy_version": CARE_PUBLICATION_PREVIOUS_POLICY_VERSION,
            "legacy_policy_version": CARE_PUBLICATION_LEGACY_POLICY_VERSION,
            "holdout_version": CARE_PUBLICATION_HOLDOUT_VERSION,
            "total_evaluated": len(opportunities),
            "currently_published": sum(
                opportunity.get("publication_status") == "PUBLISHED"
                for opportunity in opportunities
            ),
            "outcomes": {
                outcome: publication_counts[outcome]
                for outcome in (
                    "AUTO_PUBLISH_ELIGIBLE",
                    "QA_HOLDOUT",
                    "MANUAL_REVIEW",
                    "INELIGIBLE",
                    "ALREADY_PUBLISHED",
                    "MANUAL_PROTECTION",
                )
            },
            "exclusions": dict(sorted(publication_exclusions.items())),
            "outcomes_by_lifecycle": {
                outcome: dict(sorted(values.items()))
                for outcome, values in sorted(publication_by_lifecycle.items())
            },
            "outcomes_by_source_mix": {
                outcome: dict(sorted(values.items()))
                for outcome, values in sorted(publication_by_source_mix.items())
            },
            "samples": publication_samples,
            "previous_preview_outcomes_recomputed": dict(
                sorted(previous_publication_counts.items())
            ),
            "legacy_v1_preview_outcomes_recomputed": dict(
                sorted(legacy_publication_counts.items())
            ),
            "changed_since_previous_policy_preview": publication_changed_count,
            "change_transitions": dict(sorted(publication_change_transitions.items())),
            "changed_examples": publication_changed_examples,
            "published_policy_conflict_count": len(published_policy_conflicts),
            "published_policy_conflicts": published_policy_conflicts,
            "published_conflict_analysis": {
                "by_lifecycle": dict(sorted(conflict_lifecycles.items())),
                "by_source_mix": dict(sorted(conflict_source_mix.items())),
                "by_primary_reason": dict(sorted(conflict_primary_reasons.items())),
                "by_change_type": dict(sorted(conflict_change_types.items())),
                "by_planning_subtype": dict(sorted(conflict_planning_subtypes.items())),
                "by_judgement": dict(sorted(conflict_judgements.items())),
                "customer_useful": dict(sorted(conflict_customer_useful.items())),
                "recommendations": dict(sorted(conflict_recommendations.items())),
                "representative_examples": {
                    reason: deterministic_policy_samples(items, limit=5)
                    for reason, items in sorted(conflict_samples.items())
                },
            },
            "manual_review_analysis": {
                "v2_total": sum(manual_review_cohorts.values()),
                "cohorts": dict(sorted(manual_review_cohorts.items())),
                "representative_examples": {
                    cohort: deterministic_policy_samples(items, limit=5)
                    for cohort, items in sorted(manual_review_samples.items())
                },
            },
            "automation": {
                "status": "ENABLED" if publication_state[0] else "DISABLED",
                "execution_enabled": bool(publication_state[0]),
                "recurring_enabled": bool(publication_state[1]),
                "emergency_reason": publication_state[2],
                "max_publications_per_execution": int(publication_state[3]),
                "policy_version": publication_state[4],
                "last_execution_at": publication_state[5],
                "last_batch": {
                    "selected": int(publication_state[6]),
                    "published": int(publication_state[7]),
                    "skipped": int(publication_state[8]),
                    "failed": int(publication_state[9]),
                },
                "updated_at": publication_state[10],
                "automatically_published": int(publication_state[11]),
                "manually_published": int(publication_state[12]),
                "recent_failures": publication_state[13] or [],
            },
            "publication_mutations": 0,
            "withdrawal_mutations": 0,
        },
        "withdrawal_preview": dict(sorted(withdrawal_counts.items())),
        "published_lifecycle_counts": dict(sorted(published_lifecycle_counts.items())),
        "hygiene_counts": hygiene["category_counts"],
        "examples": examples,
    }


def _current_care_publication_inventory(settings: Settings) -> dict[str, Any]:
    with connection(settings) as conn:
        opportunities = _care_policy_opportunities(conn)
        failure_rows = conn.execute(
            """SELECT opportunity_id, count(*), max(created_at)
               FROM care_publication_run_items WHERE status = 'FAILED'
               GROUP BY opportunity_id"""
        ).fetchall()
    failure_state = {str(row[0]): {"count": int(row[1]), "latest": row[2]} for row in failure_rows}
    hygiene = audit_opportunities(opportunities)
    hygiene_by_id = {item["opportunity_id"]: item for item in hygiene["items"]}
    decisions: list[dict[str, Any]] = []
    outcomes: Counter[str] = Counter()
    for opportunity in opportunities:
        opportunity_id = str(opportunity["id"])
        item = hygiene_by_id[opportunity_id]
        projection, safe_title, safe_summary, decision = _care_publication_projection(
            opportunity, item
        )
        outcomes[decision.outcome] += 1
        decisions.append(
            {
                "opportunity": opportunity,
                "projection": projection,
                "hygiene": item,
                "safe_title": safe_title,
                "safe_summary": safe_summary,
                "decision": decision,
            }
        )
    eligible = [
        item
        for item in decisions
        if item["decision"].outcome == "AUTO_PUBLISH_ELIGIBLE"
        and item["opportunity"].get("publication_status") == "DRAFT"
    ]
    now = datetime.now(UTC)
    selectable = []
    deferred_failures = []
    for item in eligible:
        failure = failure_state.get(str(item["opportunity"]["id"]))
        if failure and (
            failure["count"] >= 3
            or (failure["latest"] and failure["latest"] >= now - timedelta(hours=24))
        ):
            deferred_failures.append(item)
        else:
            selectable.append(item)
    return {
        "opportunities": opportunities,
        "decisions": decisions,
        "outcomes": dict(sorted(outcomes.items())),
        "eligible": eligible,
        "selectable": selectable,
        "deferred_failures": deferred_failures,
    }


def care_policy_readonly_items(settings: Settings) -> list[dict[str, Any]]:
    """Return current policy decisions for MCP/admin projections without mutation."""
    inventory = _current_care_publication_inventory(settings)
    items: list[dict[str, Any]] = []
    for item in inventory["decisions"]:
        opportunity = item["opportunity"]
        publication = item["decision"]
        withdrawal = _care_withdrawal_decision(item)
        items.append(
            {
                "opportunity_id": str(opportunity["id"]),
                "publication_outcome": publication.outcome,
                "publication_reason": publication.reason,
                "publication_exclusions": list(publication.exclusions),
                "withdrawal_outcome": withdrawal.outcome,
                "withdrawal_reason": withdrawal.reason,
                "hygiene_category": item["hygiene"].get("category"),
                "hygiene_reason": item["hygiene"].get("reason"),
            }
        )
    return items


def _care_withdrawal_preview_from_inventory(inventory: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded Phase-D preview from an already-loaded publication inventory."""
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    by_lifecycle: dict[str, Counter[str]] = {}
    by_provenance: dict[str, Counter[str]] = {}
    samples: dict[str, list[dict[str, Any]]] = {}
    auto_candidates: list[dict[str, Any]] = []
    protected_terminal: list[dict[str, Any]] = []

    for item in inventory["decisions"]:
        opportunity = item["opportunity"]
        if opportunity.get("publication_status") != "PUBLISHED":
            continue
        projection = item["projection"]
        hygiene = item["hygiene"]
        lifecycle = str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW")
        provenance = (
            "AUTOMATIC"
            if (opportunity.get("publication_automation_provenance") or {}).get("policy_version")
            else "MANUAL_PROTECTED"
        )
        decision = evaluate_withdrawal(
            projection,
            lifecycle,
            opportunity.get("relationships") or [],
            hygiene_category=hygiene.get("category"),
            hygiene_warning=hygiene.get("warning"),
        )
        outcomes[decision.outcome] += 1
        reasons[decision.reason] += 1
        by_lifecycle.setdefault(lifecycle, Counter())[decision.outcome] += 1
        by_provenance.setdefault(provenance, Counter())[decision.outcome] += 1

        planning_outcomes = sorted(
            {
                canonical_planning_outcome(signal.get("metadata") or {}).outcome.value
                for signal in opportunity.get("relationships") or []
                if signal.get("source_type") == "planning" and signal.get("status") == "ACTIVE"
            }
        )
        evidence_dates = [
            signal.get("latest_revision_at") or signal.get("discovered_at")
            for signal in opportunity.get("relationships") or []
            if signal.get("status") == "ACTIVE"
            and (signal.get("latest_revision_at") or signal.get("discovered_at"))
        ]
        sample = {
            "opportunity_id": str(opportunity["id"]),
            "current_lifecycle": lifecycle,
            "publication_provenance": provenance,
            "outcome": decision.outcome,
            "withdrawal_reason": decision.reason,
            "supporting_evidence": {
                "foundational_signals": int(hygiene.get("foundational_signal_count") or 0),
                "supporting_followups": int(hygiene.get("supporting_signal_count") or 0),
                "source_types": sorted(hygiene.get("source_types") or []),
                "planning_outcomes": planning_outcomes,
            },
            "relevant_dates": {
                "published_at": opportunity.get("customer_published_at"),
                "latest_evidence_at": max(evidence_dates) if evidence_dates else None,
            },
        }
        outcome_samples = samples.setdefault(decision.outcome, [])
        if len(outcome_samples) < 5:
            outcome_samples.append(sample)
        if decision.outcome == "AUTO_WITHDRAW_ELIGIBLE" and len(auto_candidates) < 100:
            auto_candidates.append(sample)
        if (
            decision.outcome == "MANUAL_PROTECTION"
            and (
                lifecycle == "STOPPED"
                or opportunity.get("merged_into_opportunity_id")
                or hygiene.get("category") in {"SUPERSEDED_CANDIDATE", "DUPLICATE_CANDIDATE"}
            )
            and len(protected_terminal) < 100
        ):
            protected_terminal.append(sample)

    published_total = sum(outcomes.values())
    return {
        "preview_only": True,
        "enabled": False,
        "policy_version": CARE_WITHDRAWAL_POLICY_VERSION,
        "currently_published_total": published_total,
        "outcomes": {
            outcome: int(outcomes.get(outcome, 0))
            for outcome in (
                "KEEP_PUBLISHED",
                "AUTO_WITHDRAW_ELIGIBLE",
                "MANUAL_REVIEW",
                "MANUAL_PROTECTION",
            )
        },
        "by_lifecycle": {
            key: dict(sorted(value.items())) for key, value in sorted(by_lifecycle.items())
        },
        "by_publication_provenance": {
            key: dict(sorted(value.items())) for key, value in sorted(by_provenance.items())
        },
        "reason_counts": dict(sorted(reasons.items())),
        "automatic_withdrawal_candidates": auto_candidates,
        "manually_protected_terminal_cases": protected_terminal,
        "samples": {key: value for key, value in sorted(samples.items())},
        "changed_since_previous_preview": {
            "available": False,
            "reason": "Withdrawal preview outcomes are not persisted in Phase D1.",
        },
        "publication_state_mutations": 0,
    }


def _care_withdrawal_decision(item: dict[str, Any]) -> Any:
    opportunity = item["opportunity"]
    hygiene = item["hygiene"]
    return evaluate_withdrawal(
        item["projection"],
        str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW"),
        opportunity.get("relationships") or [],
        hygiene_category=hygiene.get("category"),
        hygiene_warning=hygiene.get("warning"),
    )


def _current_care_withdrawal_inventory(settings: Settings) -> dict[str, Any]:
    publication_inventory = _current_care_publication_inventory(settings)
    with connection(settings) as conn:
        failure_rows = conn.execute(
            """SELECT opportunity_id, count(*), max(created_at)
               FROM care_withdrawal_run_items WHERE status = 'FAILED'
               GROUP BY opportunity_id"""
        ).fetchall()
    failure_state = {str(row[0]): {"count": int(row[1]), "latest": row[2]} for row in failure_rows}
    published: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    outcomes: Counter[str] = Counter()
    for item in publication_inventory["decisions"]:
        if item["opportunity"].get("publication_status") != "PUBLISHED":
            continue
        decision = _care_withdrawal_decision(item)
        enriched = {**item, "withdrawal_decision": decision}
        published.append(enriched)
        outcomes[decision.outcome] += 1
        if decision.outcome == "AUTO_WITHDRAW_ELIGIBLE":
            eligible.append(enriched)

    now = datetime.now(UTC)
    selectable: list[dict[str, Any]] = []
    deferred_failures: list[dict[str, Any]] = []
    for item in eligible:
        failure = failure_state.get(str(item["opportunity"]["id"]))
        if failure and (
            failure["count"] >= 3
            or (failure["latest"] and failure["latest"] >= now - timedelta(hours=24))
        ):
            deferred_failures.append(item)
        else:
            selectable.append(item)
    return {
        "publication_inventory": publication_inventory,
        "published": published,
        "eligible": eligible,
        "selectable": selectable,
        "deferred_failures": deferred_failures,
        "outcomes": dict(sorted(outcomes.items())),
    }


def care_withdrawal_preview(settings: Settings, *, limit: int = 10) -> dict[str, Any]:
    """Evaluate currently published CareProspect opportunities without mutation."""
    requested_limit = min(max(int(limit), 1), 50)
    inventory = _current_care_withdrawal_inventory(settings)
    report = _care_withdrawal_preview_from_inventory(inventory["publication_inventory"])
    with connection(settings) as conn:
        state = conn.execute(
            """SELECT execution_enabled, recurring_enabled, emergency_reason,
                      max_withdrawals_per_execution, policy_version,
                      last_execution_at, last_selected, last_withdrawn,
                      last_skipped, last_failed, updated_at
               FROM care_withdrawal_automation_state WHERE singleton"""
        ).fetchone()
    selected = inventory["selectable"][:requested_limit]
    report.update(
        {
            "preview_only": not bool(state and state[0] and state[1]),
            "enabled": bool(state and state[0]),
            "runtime": {
                "execution_enabled": bool(state and state[0]),
                "recurring_enabled": bool(state and state[1]),
                "emergency_reason": state[2] if state else None,
                "max_withdrawals_per_execution": int(state[3]) if state else 10,
                "policy_version": state[4] if state else CARE_WITHDRAWAL_POLICY_VERSION,
                "last_execution_at": state[5] if state else None,
                "latest_execution": {
                    "selected": int(state[6] or 0) if state else 0,
                    "withdrawn": int(state[7] or 0) if state else 0,
                    "skipped": int(state[8] or 0) if state else 0,
                    "failed": int(state[9] or 0) if state else 0,
                },
                "updated_at": state[10] if state else None,
            },
            "eligible": len(inventory["eligible"]),
            "selectable": len(inventory["selectable"]),
            "selected": len(selected),
            "selected_opportunity_ids": [str(item["opportunity"]["id"]) for item in selected],
            "deferred_failures": len(inventory["deferred_failures"]),
            "protected_publications_selectable": 0,
        }
    )
    return report


def set_care_withdrawal_automation_execution(
    settings: Settings,
    *,
    enabled: bool,
    recurring_enabled: bool,
    actor: str,
    reason: str | None,
) -> dict[str, Any]:
    if recurring_enabled and not enabled:
        raise ValueError("recurring withdrawal requires execution to be enabled")
    with connection(settings) as conn:
        row = conn.execute(
            """UPDATE care_withdrawal_automation_state
               SET execution_enabled = %s, recurring_enabled = %s,
                   emergency_reason = %s, updated_by = %s, updated_at = now()
               WHERE singleton
               RETURNING execution_enabled, recurring_enabled, emergency_reason,
                         max_withdrawals_per_execution, policy_version, updated_at""",
            (enabled, recurring_enabled, reason, actor),
        ).fetchone()
        conn.execute(
            """INSERT INTO admin_audit_events
                 (actor, action, target_type, details, vertical)
               VALUES (%s, %s, 'withdrawal_automation', %s, 'CHILDRENS_HOME')""",
            (
                actor,
                "care_withdrawal_automation_enabled"
                if enabled
                else "care_withdrawal_automation_disabled",
                Jsonb(
                    {
                        "enabled": enabled,
                        "recurring_enabled": recurring_enabled,
                        "reason": reason,
                        "policy_version": CARE_WITHDRAWAL_POLICY_VERSION,
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "execution_enabled": bool(row[0]),
        "recurring_enabled": bool(row[1]),
        "emergency_reason": row[2],
        "max_withdrawals_per_execution": int(row[3]),
        "policy_version": row[4],
        "updated_at": row[5],
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }


def _record_care_withdrawal_run_item(
    settings: Settings,
    *,
    run_id: str,
    opportunity_id: str,
    status: str,
    policy_outcome: str | None,
    reason: str,
    lifecycle: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    with connection(settings) as conn:
        conn.execute(
            """INSERT INTO care_withdrawal_run_items
                 (run_id, opportunity_id, status, policy_outcome, reason,
                  lifecycle, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, opportunity_id) DO NOTHING""",
            (
                run_id,
                opportunity_id,
                status,
                policy_outcome,
                reason,
                lifecycle,
                Jsonb(details or {}),
            ),
        )
        conn.commit()


def execute_care_withdrawal_batch(
    settings: Settings,
    *,
    actor: str,
    limit: int = 10,
    trigger_source: str = "ADMIN",
    require_recurring: bool = False,
) -> dict[str, Any]:
    requested_limit = min(max(int(limit), 1), 50)
    with connection(settings) as conn:
        state = conn.execute(
            """SELECT execution_enabled, recurring_enabled,
                      max_withdrawals_per_execution, policy_version, emergency_reason
               FROM care_withdrawal_automation_state WHERE singleton FOR UPDATE"""
        ).fetchone()
        if not state or not state[0] or (require_recurring and not state[1]):
            conn.rollback()
            return {
                "execution_enabled": bool(state and state[0]),
                "recurring_enabled": bool(state and state[1]),
                "selected": 0,
                "withdrawn": 0,
                "skipped": 0,
                "failed": 0,
                "reason": state[4] if state else "withdrawal_state_missing",
                "publication_changes": 0,
                "withdrawal_changes": 0,
            }
        batch_limit = min(requested_limit, int(state[2]))
        run_id = str(
            conn.execute(
                """INSERT INTO care_withdrawal_runs
                     (policy_version, actor, trigger_source, status, requested_limit)
                   VALUES (%s, %s, %s, 'RUNNING', %s) RETURNING id""",
                (CARE_WITHDRAWAL_POLICY_VERSION, actor, trigger_source, batch_limit),
            ).fetchone()[0]
        )
        conn.commit()

    inventory = _current_care_withdrawal_inventory(settings)
    selected = inventory["selectable"][:batch_limit]
    result: dict[str, Any] = {
        "run_id": run_id,
        "policy_version": CARE_WITHDRAWAL_POLICY_VERSION,
        "execution_enabled": True,
        "recurring_enabled": bool(state[1]),
        "selected": len(selected),
        "withdrawn": 0,
        "skipped": 0,
        "failed": 0,
        "audit_rows_created": 0,
        "unexpected_policy_transitions": 0,
        "withdrawn_opportunity_ids": [],
        "failures": [],
        "eligible_before": len(inventory["eligible"]),
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }
    for candidate in selected:
        opportunity_id = str(candidate["opportunity"]["id"])
        lifecycle = str(candidate["opportunity"].get("customer_lifecycle_stage") or "")
        try:
            with connection(settings) as conn:
                rows = _care_policy_opportunities(conn, opportunity_id=opportunity_id)
                if not rows:
                    raise ValueError("opportunity_not_found")
                current = rows[0]
                hygiene = audit_opportunities([current])["items"][0]
                projection, _, _, _ = _care_publication_projection(current, hygiene)
                current_item = {
                    "opportunity": current,
                    "projection": projection,
                    "hygiene": hygiene,
                }
                decision = _care_withdrawal_decision(current_item)
                lifecycle = str(current.get("customer_lifecycle_stage") or "")
                if decision.outcome != "AUTO_WITHDRAW_ELIGIBLE":
                    result["skipped"] += 1
                    result["unexpected_policy_transitions"] += 1
                    conn.rollback()
                    _record_care_withdrawal_run_item(
                        settings,
                        run_id=run_id,
                        opportunity_id=opportunity_id,
                        status="SKIPPED",
                        policy_outcome=decision.outcome,
                        reason=decision.reason,
                        lifecycle=lifecycle,
                    )
                    continue
                evidence = [
                    {
                        "signal_id": str(signal.get("id")),
                        "source_type": signal.get("source_type"),
                        "planning_outcome": canonical_planning_outcome(
                            signal.get("metadata") or {}
                        ).outcome.value
                        if signal.get("source_type") == "planning"
                        else None,
                    }
                    for signal in current.get("relationships") or []
                    if signal.get("status") == "ACTIVE"
                ]
                provenance = {
                    "policy_version": CARE_WITHDRAWAL_POLICY_VERSION,
                    "policy_outcome": decision.outcome,
                    "withdrawal_reason": decision.reason,
                    "lifecycle": lifecycle,
                    "automation_actor": actor,
                    "trigger_source": trigger_source,
                    "run_id": run_id,
                    "previous_publication_status": "PUBLISHED",
                    "previous_publication_provenance": current.get(
                        "publication_automation_provenance"
                    )
                    or {},
                    "evidence": evidence,
                }
                updated = conn.execute(
                    """UPDATE opportunities
                       SET publication_status = 'WITHDRAWN',
                           customer_withdrawn_at = now(),
                           withdrawal_automation_provenance = %s,
                           updated_at = now()
                       WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                         AND publication_status = 'PUBLISHED'
                         AND NOT publication_automation_blocked
                         AND publication_automation_provenance ? 'policy_version'
                       RETURNING id, publication_status, customer_withdrawn_at""",
                    (Jsonb(provenance), opportunity_id),
                ).fetchone()
                if not updated:
                    conn.rollback()
                    result["skipped"] += 1
                    _record_care_withdrawal_run_item(
                        settings,
                        run_id=run_id,
                        opportunity_id=opportunity_id,
                        status="SKIPPED",
                        policy_outcome="STATE_CHANGED",
                        reason="conditional_withdrawal_guard_failed",
                        lifecycle=lifecycle,
                    )
                    continue
                if updated[1] != "WITHDRAWN" or not updated[2]:
                    raise ValueError("post_withdrawal_validation_failed")
                conn.execute(
                    """INSERT INTO care_withdrawal_run_items
                         (run_id, opportunity_id, status, policy_outcome,
                          reason, lifecycle, details)
                       VALUES (%s, %s, 'WITHDRAWN', %s, %s, %s, %s)""",
                    (
                        run_id,
                        opportunity_id,
                        decision.outcome,
                        decision.reason,
                        lifecycle,
                        Jsonb({"provenance": provenance, "validated": True}),
                    ),
                )
                conn.execute(
                    """INSERT INTO admin_audit_events
                         (actor, action, target_type, details, vertical)
                       VALUES (%s, 'care_opportunity_auto_withdrawn',
                               'opportunity', %s, 'CHILDRENS_HOME')""",
                    (
                        actor,
                        Jsonb(
                            {
                                "opportunity_id": opportunity_id,
                                "publication_status": "WITHDRAWN",
                                **provenance,
                            }
                        ),
                    ),
                )
                conn.commit()
            result["withdrawn"] += 1
            result["audit_rows_created"] += 1
            result["publication_changes"] += 1
            result["withdrawal_changes"] += 1
            result["withdrawn_opportunity_ids"].append(opportunity_id)
        except Exception as error:
            result["failed"] += 1
            result["failures"].append(
                {"opportunity_id": opportunity_id, "error": type(error).__name__}
            )
            _record_care_withdrawal_run_item(
                settings,
                run_id=run_id,
                opportunity_id=opportunity_id,
                status="FAILED",
                policy_outcome=None,
                reason=type(error).__name__,
                lifecycle=lifecycle,
            )

    run_status = "COMPLETED" if result["failed"] == 0 else "PARTIAL"
    with connection(settings) as conn:
        conn.execute(
            """UPDATE care_withdrawal_runs
               SET status = %s, selected_count = %s, withdrawn_count = %s,
                   skipped_count = %s, failed_count = %s, details = %s,
                   completed_at = now() WHERE id = %s""",
            (
                run_status,
                result["selected"],
                result["withdrawn"],
                result["skipped"],
                result["failed"],
                Jsonb(
                    {
                        "unexpected_policy_transitions": result["unexpected_policy_transitions"],
                        "publication_changes": result["publication_changes"],
                    }
                ),
                run_id,
            ),
        )
        conn.execute(
            """UPDATE care_withdrawal_automation_state
               SET last_execution_at = now(), last_selected = %s,
                   last_withdrawn = %s, last_skipped = %s, last_failed = %s,
                   updated_by = %s, updated_at = now() WHERE singleton""",
            (
                result["selected"],
                result["withdrawn"],
                result["skipped"],
                result["failed"],
                actor,
            ),
        )
        conn.commit()
    result["eligible_after"] = max(result["eligible_before"] - result["withdrawn"], 0)
    return result


def care_publication_automation_preview(settings: Settings, *, limit: int = 25) -> dict[str, Any]:
    requested_limit = min(max(int(limit), 1), 100)
    inventory = _current_care_publication_inventory(settings)
    eligible = inventory["eligible"]
    selected = inventory["selectable"][:requested_limit]
    return {
        "preview": True,
        "policy_version": CARE_PUBLICATION_POLICY_VERSION,
        "total_evaluated": len(inventory["opportunities"]),
        "outcomes": inventory["outcomes"],
        "eligible_unpublished": len(eligible),
        "selected": len(selected),
        "selected_opportunity_ids": [str(item["opportunity"]["id"]) for item in selected],
        "excluded_non_eligible": len(inventory["opportunities"]) - len(eligible),
        "deferred_failures": len(inventory["deferred_failures"]),
        "publication_mutations": 0,
        "withdrawal_mutations": 0,
    }


def set_care_publication_automation_execution(
    settings: Settings,
    *,
    enabled: bool,
    recurring_enabled: bool,
    actor: str,
    reason: str | None,
) -> dict[str, Any]:
    if recurring_enabled and not enabled:
        raise ValueError("recurring publication requires execution to be enabled")
    with connection(settings) as conn:
        row = conn.execute(
            """UPDATE care_publication_automation_state
               SET execution_enabled = %s, recurring_enabled = %s,
                   emergency_reason = %s, updated_by = %s, updated_at = now()
               WHERE singleton
               RETURNING execution_enabled, recurring_enabled, emergency_reason,
                         max_publications_per_execution, policy_version, updated_at""",
            (enabled, recurring_enabled, reason, actor),
        ).fetchone()
        conn.execute(
            """INSERT INTO admin_audit_events
                 (actor, action, target_type, details, vertical)
               VALUES (%s, %s, 'publication_automation', %s, 'CHILDRENS_HOME')""",
            (
                actor,
                "care_publication_automation_enabled"
                if enabled
                else "care_publication_automation_disabled",
                Jsonb(
                    {
                        "enabled": enabled,
                        "recurring_enabled": recurring_enabled,
                        "reason": reason,
                        "policy_version": CARE_PUBLICATION_POLICY_VERSION,
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "execution_enabled": bool(row[0]),
        "recurring_enabled": bool(row[1]),
        "emergency_reason": row[2],
        "max_publications_per_execution": int(row[3]),
        "policy_version": row[4],
        "updated_at": row[5],
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }


def _record_care_publication_run_item(
    settings: Settings,
    *,
    run_id: str,
    opportunity_id: str,
    status: str,
    policy_outcome: str | None,
    reason: str,
    lifecycle: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    with connection(settings) as conn:
        conn.execute(
            """INSERT INTO care_publication_run_items
                 (run_id, opportunity_id, status, policy_outcome, reason,
                  lifecycle, details)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id, opportunity_id) DO NOTHING""",
            (
                run_id,
                opportunity_id,
                status,
                policy_outcome,
                reason,
                lifecycle,
                Jsonb(details or {}),
            ),
        )
        conn.commit()


def execute_care_publication_batch(
    settings: Settings,
    *,
    actor: str,
    limit: int = 25,
    trigger_source: str = "ADMIN",
    require_recurring: bool = False,
) -> dict[str, Any]:
    requested_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        state = conn.execute(
            """SELECT execution_enabled, recurring_enabled,
                      max_publications_per_execution, policy_version, emergency_reason
               FROM care_publication_automation_state WHERE singleton FOR UPDATE"""
        ).fetchone()
        if not state or not state[0] or (require_recurring and not state[1]):
            conn.rollback()
            return {
                "execution_enabled": bool(state and state[0]),
                "recurring_enabled": bool(state and state[1]),
                "selected": 0,
                "published": 0,
                "skipped": 0,
                "failed": 0,
                "reason": state[4] if state else "publication_state_missing",
                "publication_changes": 0,
                "withdrawal_changes": 0,
            }
        batch_limit = min(requested_limit, int(state[2]))
        run_id = str(
            conn.execute(
                """INSERT INTO care_publication_runs
                     (policy_version, actor, trigger_source, status, requested_limit)
                   VALUES (%s, %s, %s, 'RUNNING', %s) RETURNING id""",
                (CARE_PUBLICATION_POLICY_VERSION, actor, trigger_source, batch_limit),
            ).fetchone()[0]
        )
        conn.commit()

    inventory = _current_care_publication_inventory(settings)
    selected = inventory["selectable"][:batch_limit]
    result = {
        "run_id": run_id,
        "policy_version": CARE_PUBLICATION_POLICY_VERSION,
        "execution_enabled": True,
        "recurring_enabled": bool(state[1]),
        "selected": len(selected),
        "published": 0,
        "skipped": 0,
        "failed": 0,
        "audit_rows_created": 0,
        "unexpected_policy_transitions": 0,
        "published_opportunity_ids": [],
        "failures": [],
        "eligible_unpublished_before": len(inventory["eligible"]),
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }
    for candidate in selected:
        opportunity_id = str(candidate["opportunity"]["id"])
        lifecycle = str(candidate["opportunity"].get("customer_lifecycle_stage") or "")
        try:
            with connection(settings) as conn:
                current_rows = _care_policy_opportunities(conn, opportunity_id=opportunity_id)
                if not current_rows:
                    raise ValueError("opportunity_not_found")
                current = current_rows[0]
                hygiene_item = audit_opportunities([current])["items"][0]
                projection, safe_title, safe_summary, decision = _care_publication_projection(
                    current, hygiene_item
                )
                lifecycle = str(current.get("customer_lifecycle_stage") or "")
                if decision.outcome != "AUTO_PUBLISH_ELIGIBLE":
                    result["skipped"] += 1
                    result["unexpected_policy_transitions"] += 1
                    conn.rollback()
                    _record_care_publication_run_item(
                        settings,
                        run_id=run_id,
                        opportunity_id=opportunity_id,
                        status="SKIPPED",
                        policy_outcome=decision.outcome,
                        reason=decision.reason,
                        lifecycle=lifecycle,
                        details={"exclusions": list(decision.exclusions)},
                    )
                    continue
                evidence = [
                    {
                        "signal_id": str(signal.get("id")),
                        "source_type": signal.get("source_type"),
                        "planning_subtype": (signal.get("extracted_facts") or {}).get(
                            "planning_subtype"
                        ),
                        "opportunity_creation_decision": (signal.get("extracted_facts") or {}).get(
                            "opportunity_creation_decision"
                        ),
                    }
                    for signal in current.get("relationships") or []
                    if signal.get("status") == "ACTIVE"
                ]
                provenance = {
                    "policy_version": CARE_PUBLICATION_POLICY_VERSION,
                    "holdout_version": CARE_PUBLICATION_HOLDOUT_VERSION,
                    "policy_outcome": decision.outcome,
                    "policy_reason": decision.reason,
                    "lifecycle": lifecycle,
                    "automation_actor": actor,
                    "trigger_source": trigger_source,
                    "run_id": run_id,
                    "evidence": evidence,
                }
                updated = conn.execute(
                    """UPDATE opportunities
                       SET publication_status = 'PUBLISHED',
                           customer_title = COALESCE(customer_title, %s),
                           customer_summary = COALESCE(customer_summary, %s),
                           customer_published_by = %s,
                           customer_published_at = now(),
                           publication_automation_provenance = %s,
                           updated_at = now()
                       WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                         AND publication_status = 'DRAFT'
                         AND NOT publication_automation_blocked
                         AND review_status NOT IN ('MERGED', 'REJECTED')
                         AND merged_into_opportunity_id IS NULL
                       RETURNING id, publication_status, customer_title,
                                 customer_summary, customer_published_at""",
                    (
                        safe_title,
                        safe_summary,
                        "SYSTEM_PUBLICATION_COORDINATOR",
                        Jsonb(provenance),
                        opportunity_id,
                    ),
                ).fetchone()
                if not updated:
                    conn.rollback()
                    result["skipped"] += 1
                    _record_care_publication_run_item(
                        settings,
                        run_id=run_id,
                        opportunity_id=opportunity_id,
                        status="SKIPPED",
                        policy_outcome="STATE_CHANGED",
                        reason="conditional_publication_guard_failed",
                        lifecycle=lifecycle,
                    )
                    continue
                if not updated[2] or not updated[3] or updated[1] != "PUBLISHED":
                    raise ValueError("post_publication_validation_failed")
                conn.execute(
                    """INSERT INTO care_publication_run_items
                         (run_id, opportunity_id, status, policy_outcome,
                          reason, lifecycle, details)
                       VALUES (%s, %s, 'PUBLISHED', %s, %s, %s, %s)""",
                    (
                        run_id,
                        opportunity_id,
                        decision.outcome,
                        decision.reason,
                        lifecycle,
                        Jsonb({"provenance": provenance, "validated": True}),
                    ),
                )
                conn.execute(
                    """INSERT INTO admin_audit_events
                         (actor, action, target_type, details, vertical)
                       VALUES (%s, 'care_opportunity_auto_published',
                               'opportunity', %s, 'CHILDRENS_HOME')""",
                    (
                        actor,
                        Jsonb(
                            {
                                "opportunity_id": opportunity_id,
                                "previous_publication_status": "DRAFT",
                                "publication_status": "PUBLISHED",
                                **provenance,
                            }
                        ),
                    ),
                )
                conn.commit()
            result["published"] += 1
            result["audit_rows_created"] += 1
            result["publication_changes"] += 1
            result["published_opportunity_ids"].append(opportunity_id)
        except Exception as error:
            result["failed"] += 1
            result["failures"].append(
                {"opportunity_id": opportunity_id, "error": type(error).__name__}
            )
            _record_care_publication_run_item(
                settings,
                run_id=run_id,
                opportunity_id=opportunity_id,
                status="FAILED",
                policy_outcome=None,
                reason=type(error).__name__,
                lifecycle=lifecycle,
            )

    run_status = "COMPLETED" if result["failed"] == 0 else "PARTIAL"
    with connection(settings) as conn:
        conn.execute(
            """UPDATE care_publication_runs
               SET status = %s, selected_count = %s, published_count = %s,
                   skipped_count = %s, failed_count = %s, details = %s,
                   completed_at = now() WHERE id = %s""",
            (
                run_status,
                result["selected"],
                result["published"],
                result["skipped"],
                result["failed"],
                Jsonb(
                    {
                        "unexpected_policy_transitions": result["unexpected_policy_transitions"],
                        "withdrawal_changes": 0,
                    }
                ),
                run_id,
            ),
        )
        conn.execute(
            """UPDATE care_publication_automation_state
               SET last_execution_at = now(), last_selected = %s,
                   last_published = %s, last_skipped = %s, last_failed = %s,
                   updated_by = %s, updated_at = now() WHERE singleton""",
            (
                result["selected"],
                result["published"],
                result["skipped"],
                result["failed"],
                actor,
            ),
        )
        conn.commit()
    result["eligible_unpublished_after"] = max(
        result["eligible_unpublished_before"] - result["published"], 0
    )
    return result


def _care_planning_watch_candidates(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT o.id, o.customer_lifecycle_stage, o.publication_automation_blocked,
                  o.address, o.postcode, rs.id, rs.external_id, rs.discovered_at,
                  rs.metadata, se.review_status, se.extracted_facts, os.status,
                  os.extracted_facts,
                  COALESCE((SELECT jsonb_agg(DISTINCT pfr.relationship_type)
                    FROM planning_signal_family_relationships pfr
                    WHERE pfr.raw_signal_id = rs.id), '[]'::jsonb),
                  (SELECT pfr.family_id FROM planning_signal_family_relationships pfr
                    WHERE pfr.raw_signal_id = rs.id
                    ORDER BY (pfr.relationship_type = 'PRIMARY_APPLICATION') DESC,
                             pfr.created_at, pfr.family_id LIMIT 1),
                  (SELECT max(rev.observed_at) FROM raw_signal_revisions rev
                    WHERE rev.raw_signal_id = rs.id)
           FROM opportunities o
           JOIN opportunity_signals os ON os.opportunity_id = o.id AND os.status = 'ACTIVE'
           JOIN raw_signals rs ON rs.id = os.raw_signal_id AND rs.source_type = 'planning'
           LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
           WHERE o.vertical = 'CHILDRENS_HOME'
             AND o.review_status NOT IN ('MERGED', 'REJECTED')
           ORDER BY o.id, rs.id"""
    ).fetchall()
    fields = (
        "opportunity_id",
        "customer_lifecycle_stage",
        "publication_automation_blocked",
        "site_address",
        "site_postcode",
        "id",
        "external_id",
        "discovered_at",
        "metadata",
        "review_status",
        "extracted_facts",
        "relationship_status",
        "relationship_extracted_facts",
        "planning_family_relationship_types",
        "family_id",
        "latest_revision_at",
    )
    return [dict(zip(fields, row)) for row in rows]


def enrol_care_planning_watches(
    settings: Settings,
    *,
    actor: str,
    preview: bool = True,
    activated_at: datetime | None = None,
) -> dict[str, Any]:
    """Synchronise the complete v2 watch cohort without calling the provider."""
    now = (activated_at or datetime.now(UTC)).astimezone(UTC)
    with connection(settings) as conn:
        candidates = _care_planning_watch_candidates(conn)
        existing_rows = conn.execute(
            """SELECT id, opportunity_id, primary_signal_id, enabled, cadence_days,
                      next_eligible_refresh_at
               FROM planning_lifecycle_watches"""
        ).fetchall()
        existing = {
            (str(row[1]), str(row[2])): {
                "id": row[0],
                "enabled": bool(row[3]),
                "cadence_days": row[4],
                "next_poll_at": row[5],
            }
            for row in existing_rows
        }
        eligible: dict[tuple[str, str], dict[str, Any]] = {}
        exclusions: Counter[str] = Counter()
        for signal in candidates:
            opportunity = {
                "id": signal["opportunity_id"],
                "customer_lifecycle_stage": signal["customer_lifecycle_stage"],
                "publication_automation_blocked": signal["publication_automation_blocked"],
            }
            authority = planning_authority(signal.get("metadata") or {})
            reference = primary_planning_reference(
                signal.get("external_id"), signal.get("metadata") or {}
            )
            decision = evaluate_planning_watch(
                opportunity,
                {**signal, "source_type": "planning", "status": signal["relationship_status"]},
                has_planning_identity=bool(authority and reference),
                now=now,
            )
            key = (str(signal["opportunity_id"]), str(signal["id"]))
            if not decision.eligible:
                exclusions[decision.reason] += 1
                continue
            eligible[key] = {
                **signal,
                "authority": authority,
                "reference": reference,
                "decision": decision,
                "snapshot": planning_watch_snapshot(signal.get("metadata") or {}),
            }

        cadence_counts = Counter(
            f"{item['decision'].cadence_days}_days" for item in eligible.values()
        )
        projected_daily, projected_monthly = project_planning_watch_requests(cadence_counts)
        preview_next = [
            deterministic_initial_poll_at(f"{key[0]}:{key[1]}", item["decision"].cadence_days, now)
            for key, item in eligible.items()
        ]
        result = {
            "preview": preview,
            "policy_version": CARE_PLANNING_WATCHER_POLICY_VERSION,
            "candidates_evaluated": len(candidates),
            "eligible": len(eligible),
            "exclusions": dict(sorted(exclusions.items())),
            "cadence_counts": dict(sorted(cadence_counts.items())),
            "projected_requests_per_day": projected_daily,
            "projected_requests_per_30_days": projected_monthly,
            "earliest_next_poll_at": min(preview_next).isoformat() if preview_next else None,
            "latest_next_poll_at": max(preview_next).isoformat() if preview_next else None,
            "due_first_24_hours": sum(item <= now + timedelta(days=1) for item in preview_next),
            "due_first_7_days": sum(item <= now + timedelta(days=7) for item in preview_next),
            "created": 0,
            "updated": 0,
            "enabled": len(eligible),
            "disabled": 0,
            "history_rows_created": 0,
            "provider_requests": 0,
        }
        if preview:
            conn.rollback()
            return result

        for key, item in eligible.items():
            prior = existing.get(key)
            cadence = int(item["decision"].cadence_days)
            next_poll = (
                prior["next_poll_at"]
                if prior and prior["enabled"] and prior["cadence_days"] == cadence
                else deterministic_initial_poll_at(f"{key[0]}:{key[1]}", cadence, now)
            )
            if prior:
                changed = not prior["enabled"] or prior["cadence_days"] != cadence
                conn.execute(
                    """UPDATE planning_lifecycle_watches
                       SET family_id = %s, planning_authority = %s, planning_reference = %s,
                           lifecycle_at_enrolment = %s, cadence_days = %s,
                           next_eligible_refresh_at = %s, latest_outcome = %s,
                           activity_at = %s, activity_source = %s,
                           latest_status_metadata = %s, enabled = TRUE,
                           disabled_reason = NULL, policy_version = %s, updated_at = now()
                       WHERE id = %s""",
                    (
                        item.get("family_id"),
                        item["authority"],
                        item["reference"],
                        item["customer_lifecycle_stage"],
                        cadence,
                        next_poll,
                        item["decision"].planning_outcome,
                        now - timedelta(days=item["decision"].age_days)
                        if item["decision"].age_days is not None
                        else None,
                        item["decision"].age_source,
                        Jsonb(item["snapshot"]),
                        CARE_PLANNING_WATCHER_POLICY_VERSION,
                        prior["id"],
                    ),
                )
                if changed:
                    result["updated"] += 1
                    conn.execute(
                        """INSERT INTO planning_lifecycle_watch_history
                             (watch_id, action, old_enabled, new_enabled,
                              old_cadence_days, new_cadence_days, reason,
                              policy_version, actor)
                           VALUES (%s, 'UPDATED', %s, TRUE, %s, %s,
                                   'policy_reenrolment', %s, %s)""",
                        (
                            prior["id"],
                            prior["enabled"],
                            prior["cadence_days"],
                            cadence,
                            CARE_PLANNING_WATCHER_POLICY_VERSION,
                            actor,
                        ),
                    )
                    result["history_rows_created"] += 1
                continue
            watch_id = conn.execute(
                """INSERT INTO planning_lifecycle_watches
                     (family_id, opportunity_id, primary_signal_id, latest_outcome,
                      next_eligible_refresh_at, enabled, policy_version,
                      planning_authority, planning_reference, lifecycle_at_enrolment,
                      cadence_days, activity_at, activity_source,
                      latest_status_metadata, enrolment_metadata)
                   VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    item.get("family_id"),
                    item["opportunity_id"],
                    item["id"],
                    item["decision"].planning_outcome,
                    next_poll,
                    CARE_PLANNING_WATCHER_POLICY_VERSION,
                    item["authority"],
                    item["reference"],
                    item["customer_lifecycle_stage"],
                    cadence,
                    now - timedelta(days=item["decision"].age_days)
                    if item["decision"].age_days is not None
                    else None,
                    item["decision"].age_source,
                    Jsonb(item["snapshot"]),
                    Jsonb({"actor": actor, "staggered_at": now.isoformat()}),
                ),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO planning_lifecycle_watch_history
                     (watch_id, action, new_enabled, new_cadence_days, reason,
                      policy_version, actor)
                   VALUES (%s, 'ENROLLED', TRUE, %s, 'initial_policy_enrolment', %s, %s)""",
                (watch_id, cadence, CARE_PLANNING_WATCHER_POLICY_VERSION, actor),
            )
            result["created"] += 1
            result["history_rows_created"] += 1

        for key, prior in existing.items():
            if key in eligible or not prior["enabled"]:
                continue
            conn.execute(
                """UPDATE planning_lifecycle_watches
                   SET enabled = FALSE, disabled_reason = 'policy_ineligible', updated_at = now()
                   WHERE id = %s""",
                (prior["id"],),
            )
            conn.execute(
                """INSERT INTO planning_lifecycle_watch_history
                     (watch_id, action, old_enabled, new_enabled, old_cadence_days,
                      reason, policy_version, actor)
                   VALUES (%s, 'DISABLED', TRUE, FALSE, %s,
                           'policy_ineligible', %s, %s)""",
                (prior["id"], prior["cadence_days"], CARE_PLANNING_WATCHER_POLICY_VERSION, actor),
            )
            result["disabled"] += 1
            result["history_rows_created"] += 1
        if result["created"] or result["updated"] or result["disabled"]:
            conn.execute(
                """INSERT INTO admin_audit_events (actor, action, target_type, details)
                   VALUES (%s, 'care_planning_watcher_enrolment',
                           'planning_watcher', %s)""",
                (actor, Jsonb(result)),
            )
        conn.commit()
        return result


def set_care_planning_watcher_execution(
    settings: Settings, *, enabled: bool, actor: str, reason: str | None
) -> dict[str, Any]:
    """Emergency/activation switch; watch evidence and lifecycle remain untouched."""
    with connection(settings) as conn:
        row = conn.execute(
            """UPDATE planning_lifecycle_watcher_state
               SET execution_enabled = %s, emergency_reason = %s,
                   updated_by = %s, updated_at = now()
               WHERE singleton RETURNING execution_enabled, emergency_reason,
                   max_polls_per_execution, max_provider_requests_per_day,
                   max_provider_requests_per_month, policy_version, updated_at""",
            (enabled, reason, actor),
        ).fetchone()
        conn.execute(
            """INSERT INTO admin_audit_events (actor, action, target_type, details)
               VALUES (%s, %s, 'planning_watcher', %s)""",
            (
                actor,
                "care_planning_watcher_enabled" if enabled else "care_planning_watcher_disabled",
                Jsonb({"enabled": enabled, "reason": reason}),
            ),
        )
        conn.commit()
    return {
        "execution_enabled": bool(row[0]),
        "emergency_reason": row[1],
        "max_polls_per_execution": row[2],
        "max_provider_requests_per_day": row[3],
        "max_provider_requests_per_month": row[4],
        "policy_version": row[5],
        "updated_at": row[6],
        "lifecycle_changes": 0,
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }


def queue_due_care_planning_watches(
    settings: Settings,
    *,
    max_due: int = 15,
    retry_run_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Reserve and queue due watches within daily/monthly request guardrails."""
    if not settings.planning_manual_run_queue_url:
        raise RuntimeError("PLANNING_MANUAL_RUN_QUEUE_URL is not configured")
    retry_ids = tuple(UUID(value) for value in (retry_run_ids or []))
    if len(retry_ids) > 10:
        raise ValueError("retry_run_ids is limited to 10 failed runs")
    requested_limit = min(max(int(max_due), 1), 100)
    with connection(settings) as conn:
        state = conn.execute(
            """SELECT execution_enabled, emergency_reason, max_polls_per_execution,
                      max_provider_requests_per_day, max_provider_requests_per_month,
                      policy_version
               FROM planning_lifecycle_watcher_state WHERE singleton FOR UPDATE"""
        ).fetchone()
        if not state or not state[0]:
            conn.rollback()
            return {
                "execution_enabled": False,
                "queued": 0,
                "reason": state[1] if state else "watcher_state_missing",
                "provider_requests_queued": 0,
            }
        conn.execute(
            """UPDATE planning_lifecycle_watch_runs
               SET status = 'FAILED', error_category = 'callback_timeout',
                   completed_at = now()
               WHERE status IN ('QUEUED', 'RUNNING')
                 AND created_at < now() - interval '30 minutes'"""
        )
        usage = conn.execute(
            """SELECT
                 COALESCE(sum(provider_requests) FILTER (
                   WHERE created_at >=
                     date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                 ), 0),
                 COALESCE(sum(provider_requests) FILTER (
                   WHERE created_at >=
                     date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                 ), 0),
                 count(*) FILTER (WHERE status IN ('QUEUED', 'RUNNING'))
               FROM planning_lifecycle_watch_runs"""
        ).fetchone()
        today_requests, month_requests, reserved = map(int, usage)
        daily_available = max(int(state[3]) - today_requests - reserved, 0)
        monthly_available = max(int(state[4]) - month_requests - reserved, 0)
        limit = min(requested_limit, int(state[2]), daily_available, monthly_available)
        if limit <= 0:
            conn.rollback()
            return {
                "execution_enabled": True,
                "queued": 0,
                "reason": "quota_guardrail_reached",
                "requests_today": today_requests,
                "requests_this_month": month_requests,
                "reserved": reserved,
                "provider_requests_queued": 0,
            }
        retry_clause = (
            """EXISTS (
                   SELECT 1 FROM planning_lifecycle_watch_runs failed_run
                   WHERE failed_run.id = ANY(%s)
                     AND failed_run.watch_id = w.id
                     AND failed_run.status IN ('FAILED', 'RATE_LIMITED', 'QUEUE_FAILED')
                 )"""
            if retry_ids
            else "w.next_eligible_refresh_at <= now()"
        )
        query_params: tuple[Any, ...] = (list(retry_ids), limit) if retry_ids else (limit,)
        rows = conn.execute(
            f"""SELECT w.id, w.opportunity_id, w.primary_signal_id,
                      w.planning_authority, w.planning_reference,
                      w.latest_status_metadata, o.address, o.postcode,
                      rs.external_id
               FROM planning_lifecycle_watches w
               JOIN opportunities o ON o.id = w.opportunity_id
               JOIN raw_signals rs ON rs.id = w.primary_signal_id
               WHERE w.enabled AND {retry_clause}
                 AND NOT o.publication_automation_blocked
                 AND NOT EXISTS (
                   SELECT 1 FROM planning_lifecycle_watch_runs run
                   WHERE run.watch_id = w.id AND run.status IN ('QUEUED', 'RUNNING')
                 )
               ORDER BY w.next_eligible_refresh_at, w.id
               FOR UPDATE OF w SKIP LOCKED LIMIT %s""",
            query_params,
        ).fetchall()
        queued: list[dict[str, Any]] = []
        for row in rows:
            run_id = conn.execute(
                """INSERT INTO planning_lifecycle_watch_runs (watch_id, status, details)
                   VALUES (%s, 'QUEUED', %s) RETURNING id""",
                (
                    row[0],
                    Jsonb(
                        {
                            "policy_version": state[5],
                            "retry_of_failed_run": bool(retry_ids),
                        }
                    ),
                ),
            ).fetchone()[0]
            item = {
                "invocation_source": "planning_lifecycle_watch",
                "run_id": str(run_id),
                "watch_id": str(row[0]),
                "opportunity_id": str(row[1]),
                "primary_signal_id": str(row[2]),
                "planning_authority": row[3],
                "planning_reference": row[4],
                "latest_snapshot": row[5] or {},
                "site_address": row[6],
                "site_postcode": row[7],
            }
            external_id = str(row[8] or "")
            if external_id.lower().startswith("plota:"):
                provider_application_id = external_id.split(":", 1)[1].strip()
                if provider_application_id:
                    item["provider_application_id"] = provider_application_id
            queued.append(item)
        conn.commit()

    sent = 0
    failures = 0
    for item in queued:
        try:
            response = boto3.client("sqs").send_message(
                QueueUrl=settings.planning_manual_run_queue_url,
                MessageBody=json.dumps(item, separators=(",", ":"), sort_keys=True),
            )
            if not response.get("MessageId"):
                raise RuntimeError("Planning watch message was not accepted")
            sent += 1
        except Exception as error:
            failures += 1
            with connection(settings) as conn:
                conn.execute(
                    """UPDATE planning_lifecycle_watch_runs
                       SET status = 'QUEUE_FAILED', error_category = %s,
                           details = details || %s,
                           completed_at = now() WHERE id = %s AND status = 'QUEUED'""",
                    (
                        "queue_dispatch_error",
                        Jsonb({"exception_type": type(error).__name__}),
                        item["run_id"],
                    ),
                )
                conn.commit()
    return {
        "execution_enabled": True,
        "selected": len(queued),
        "queued": sent,
        "queue_failures": failures,
        "provider_requests_queued": sent,
        "requests_today_before": today_requests,
        "requests_this_month_before": month_requests,
        "daily_limit": int(state[3]),
        "monthly_limit": int(state[4]),
        "max_polls_per_execution": int(state[2]),
    }


def update_care_planning_watch_result(
    settings: Settings,
    *,
    run_id: str,
    watch_id: str,
    status: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Persist one collector result exactly once and schedule/stop the watch."""
    if status not in {"UNCHANGED", "CHANGED", "FAILED", "RATE_LIMITED"}:
        raise ValueError("invalid Planning watch status")
    requests = max(int(details.get("provider_requests") or 0), 0)
    with connection(settings) as conn:
        run = conn.execute(
            """SELECT status FROM planning_lifecycle_watch_runs
               WHERE id = %s AND watch_id = %s FOR UPDATE""",
            (run_id, watch_id),
        ).fetchone()
        if not run:
            raise ValueError("Planning watch run not found")
        if run[0] not in {"QUEUED", "RUNNING"}:
            conn.rollback()
            return {"updated": False, "duplicate": True, "status": run[0]}
        watch = conn.execute(
            """SELECT enabled, cadence_days, consecutive_provider_errors
               FROM planning_lifecycle_watches WHERE id = %s FOR UPDATE""",
            (watch_id,),
        ).fetchone()
        if not watch:
            raise ValueError("Planning watch not found")
        latest_snapshot = details.get("latest_snapshot")
        snapshot = latest_snapshot if isinstance(latest_snapshot, dict) else None
        terminal = bool(
            snapshot
            and snapshot.get("canonical_outcome")
            in {"APPROVED", "REFUSED", "WITHDRAWN", "APPEAL_ALLOWED", "APPEAL_DISMISSED"}
        )
        errors = int(watch[2] or 0)
        if status in {"FAILED", "RATE_LIMITED"}:
            errors += 1
            next_poll = datetime.now(UTC) + timedelta(hours=min(2**errors, 24))
            enabled = bool(watch[0])
            disabled_reason = None
        elif terminal:
            errors = 0
            next_poll = datetime.now(UTC) + timedelta(days=int(watch[1] or 30))
            enabled = False
            disabled_reason = "terminal_planning_outcome"
        else:
            errors = 0
            next_poll = datetime.now(UTC) + timedelta(days=int(watch[1] or 30))
            enabled = bool(watch[0])
            disabled_reason = None
        conn.execute(
            """UPDATE planning_lifecycle_watch_runs
               SET status = %s, provider_requests = %s,
                   provider_result = %s, error_category = %s,
                   details = details || %s, started_at = COALESCE(started_at, now()),
                   completed_at = now()
               WHERE id = %s""",
            (
                status,
                requests,
                details.get("provider_result"),
                details.get("error_category") or details.get("error_type"),
                Jsonb(details),
                run_id,
            ),
        )
        conn.execute(
            """UPDATE planning_lifecycle_watches
               SET enabled = %s, disabled_reason = %s,
                   last_checked_at = now(),
                   last_poll_started_at = COALESCE(last_poll_started_at, now()),
                   last_poll_completed_at = now(), next_eligible_refresh_at = %s,
                   last_provider_result = %s, consecutive_provider_errors = %s,
                   latest_outcome = COALESCE(%s, latest_outcome),
                   latest_status_metadata = COALESCE(%s, latest_status_metadata),
                   status_changed_at = CASE
                     WHEN %s = 'CHANGED' THEN now() ELSE status_changed_at END,
                   updated_at = now()
               WHERE id = %s""",
            (
                enabled,
                disabled_reason,
                next_poll,
                status,
                errors,
                snapshot.get("canonical_outcome") if snapshot else None,
                Jsonb(snapshot) if snapshot else None,
                status,
                watch_id,
            ),
        )
        if terminal and watch[0]:
            conn.execute(
                """INSERT INTO planning_lifecycle_watch_history
                     (watch_id, action, old_enabled, new_enabled, old_cadence_days,
                      new_cadence_days, reason, policy_version, actor, details)
                   VALUES (%s, 'DISABLED', TRUE, FALSE, %s, %s,
                           'terminal_planning_outcome', %s, 'SYSTEM_WATCHER', %s)""",
                (
                    watch_id,
                    watch[1],
                    watch[1],
                    CARE_PLANNING_WATCHER_POLICY_VERSION,
                    Jsonb({"run_id": run_id, "status": status}),
                ),
            )
        conn.commit()
    return {
        "updated": True,
        "duplicate": False,
        "status": status,
        "provider_requests": requests,
        "watch_enabled": enabled,
        "watch_disabled": terminal,
    }


def recompute_care_opportunity_lifecycle_for_signal(
    settings: Settings, signal_id: str
) -> dict[str, int]:
    """Recompute linked Care lifecycles after normal enrichment, without publishing."""
    counts = {"opportunities_examined": 0, "lifecycle_changes": 0, "watches_updated": 0}
    with connection(settings) as conn:
        opportunity_rows = conn.execute(
            """SELECT DISTINCT o.id, o.customer_lifecycle_stage
               FROM opportunities o
               JOIN opportunity_signals os ON os.opportunity_id = o.id
               WHERE os.raw_signal_id = %s AND o.vertical = 'CHILDRENS_HOME'
               ORDER BY o.id""",
            (signal_id,),
        ).fetchall()
        for opportunity_id, old_lifecycle in opportunity_rows:
            relationship_rows = conn.execute(
                """SELECT rs.id, os.status, rs.source_type, rs.metadata,
                          se.review_status, se.extracted_facts,
                          COALESCE((SELECT jsonb_agg(DISTINCT pfr.relationship_type)
                            FROM planning_signal_family_relationships pfr
                            WHERE pfr.raw_signal_id = rs.id), '[]'::jsonb)
                   FROM opportunity_signals os
                   JOIN raw_signals rs ON rs.id = os.raw_signal_id
                   LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                   WHERE os.opportunity_id = %s
                   ORDER BY rs.discovered_at, rs.id""",
                (opportunity_id,),
            ).fetchall()
            signals = [
                {
                    "id": str(row[0]),
                    "status": row[1],
                    "relationship_status": row[1],
                    "source_type": row[2],
                    "metadata": row[3] or {},
                    "review_status": row[4],
                    "extracted_facts": row[5] or {},
                    "planning_family_relationship_types": row[6] or [],
                }
                for row in relationship_rows
            ]
            counts["opportunities_examined"] += 1
            decision = derive_care_lifecycle(signals)
            if decision.lifecycle.value != old_lifecycle:
                conn.execute(
                    """UPDATE opportunities
                       SET customer_lifecycle_stage = %s,
                           customer_lifecycle_reason = %s,
                           customer_lifecycle_policy_version = %s,
                           customer_lifecycle_evaluated_at = now(), updated_at = now()
                       WHERE id = %s""",
                    (
                        decision.lifecycle.value,
                        decision.reason,
                        CARE_LIFECYCLE_POLICY_VERSION,
                        opportunity_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO opportunity_lifecycle_history
                         (opportunity_id, old_lifecycle, new_lifecycle, reason,
                          triggering_signal_ids, source_type, policy_version,
                          actor_type, actor)
                       VALUES (%s, %s, %s, %s, %s, 'planning', %s,
                               'AUTOMATED', 'SYSTEM_PLANNING_WATCHER')""",
                    (
                        opportunity_id,
                        old_lifecycle,
                        decision.lifecycle.value,
                        decision.reason,
                        [UUID(value) for value in decision.triggering_signal_ids],
                        CARE_LIFECYCLE_POLICY_VERSION,
                    ),
                )
                counts["lifecycle_changes"] += 1
            if decision.lifecycle == CareLifecycle.STOPPED:
                changed = conn.execute(
                    """UPDATE planning_lifecycle_watches
                       SET enabled = FALSE, disabled_reason = 'lifecycle_terminal',
                           updated_at = now()
                       WHERE opportunity_id = %s AND enabled RETURNING id, cadence_days""",
                    (opportunity_id,),
                ).fetchall()
                for watch_id, cadence in changed:
                    conn.execute(
                        """INSERT INTO planning_lifecycle_watch_history
                             (watch_id, action, old_enabled, new_enabled,
                              old_cadence_days, new_cadence_days, reason,
                              policy_version, actor)
                           VALUES (%s, 'DISABLED', TRUE, FALSE, %s, %s,
                                   'lifecycle_terminal', %s, 'SYSTEM_WATCHER')""",
                        (watch_id, cadence, cadence, CARE_PLANNING_WATCHER_POLICY_VERSION),
                    )
                counts["watches_updated"] += len(changed)
        conn.commit()
    return counts


def bootstrap_care_opportunity_lifecycles(
    settings: Settings,
    *,
    actor: str,
    limit: int = 100,
    preview: bool = True,
) -> dict[str, Any]:
    """Persist one bounded, deterministic batch of previously unset Care lifecycles."""
    bounded_limit = min(max(int(limit), 1), 100)
    with connection(settings) as conn:
        before = conn.execute(
            """SELECT count(*) FILTER (WHERE customer_lifecycle_stage IS NULL),
                      count(*) FILTER (WHERE customer_lifecycle_stage IS NOT NULL)
               FROM opportunities WHERE vertical = 'CHILDRENS_HOME'"""
        ).fetchone()
        rows = conn.execute(
            """SELECT o.id, o.customer_lifecycle_stage,
                      COALESCE(rel.relationships, '[]'::jsonb)
               FROM opportunities o
               LEFT JOIN LATERAL (
                 SELECT jsonb_agg(jsonb_build_object(
                   'id', rs.id, 'signal_id', rs.id,
                   'status', os.status, 'relationship_status', os.status,
                   'source_type', rs.source_type, 'metadata', rs.metadata,
                   'review_status', se.review_status,
                   'extracted_facts', se.extracted_facts,
                   'relationship_extracted_facts', os.extracted_facts,
                   'planning_family_relationship_types', COALESCE(
                     (SELECT jsonb_agg(DISTINCT pfr.relationship_type)
                      FROM planning_signal_family_relationships pfr
                      WHERE pfr.raw_signal_id = rs.id), '[]'::jsonb)
                 ) ORDER BY rs.discovered_at, rs.id) AS relationships
                 FROM opportunity_signals os
                 JOIN raw_signals rs ON rs.id = os.raw_signal_id
                 LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                 WHERE os.opportunity_id = o.id
               ) rel ON TRUE
               WHERE o.vertical = 'CHILDRENS_HOME'
                 AND o.customer_lifecycle_stage IS NULL
               ORDER BY o.id
               LIMIT %s""",
            (bounded_limit,),
        ).fetchall()
        opportunities = [
            {
                "id": str(row[0]),
                "customer_lifecycle_stage": row[1],
                "signals": row[2] or [],
            }
            for row in rows
        ]

        def persist(opportunity: dict[str, Any], decision: LifecycleDecision) -> bool:
            try:
                updated = conn.execute(
                    """UPDATE opportunities
                       SET customer_lifecycle_stage = %s,
                           customer_lifecycle_reason = %s,
                           customer_lifecycle_policy_version = %s,
                           customer_lifecycle_evaluated_at = now(),
                           updated_at = now()
                       WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                         AND customer_lifecycle_stage IS NULL
                       RETURNING id""",
                    (
                        decision.lifecycle.value,
                        decision.reason,
                        CARE_LIFECYCLE_POLICY_VERSION,
                        opportunity["id"],
                    ),
                ).fetchone()
                if not updated:
                    conn.rollback()
                    return False
                source_type = {
                    CareLifecycle.REGISTERED: "ofsted",
                    CareLifecycle.REGISTRATION_DETECTED: "ofsted",
                    CareLifecycle.DELIVERY_SIGNAL_DETECTED: "recruitment",
                    CareLifecycle.PLANNING_APPROVED: "planning",
                    CareLifecycle.PLANNING_PENDING: "planning",
                    CareLifecycle.APPEAL_PENDING: "planning",
                    CareLifecycle.STOPPED: "planning",
                }.get(decision.lifecycle)
                conn.execute(
                    """INSERT INTO opportunity_lifecycle_history
                       (opportunity_id, old_lifecycle, new_lifecycle, reason,
                        triggering_signal_ids, source_type, policy_version,
                        actor_type, actor)
                       VALUES (%s, NULL, %s, %s, %s, %s, %s, 'BOOTSTRAP', %s)""",
                    (
                        opportunity["id"],
                        decision.lifecycle.value,
                        decision.reason,
                        [UUID(value) for value in decision.triggering_signal_ids],
                        source_type,
                        CARE_LIFECYCLE_POLICY_VERSION,
                        actor,
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

        result = bootstrap_lifecycle_records(
            opportunities,
            preview=preview,
            persist=persist,
        )
        remaining = conn.execute(
            """SELECT count(*) FROM opportunities
               WHERE vertical = 'CHILDRENS_HOME'
                 AND customer_lifecycle_stage IS NULL"""
        ).fetchone()[0]
        if not preview and (result["persisted"] or result["failed"]):
            conn.execute(
                """INSERT INTO admin_audit_events
                   (action, actor, target_type, details, vertical)
                   VALUES ('care_lifecycle_bootstrap_batch', %s,
                           'opportunity_lifecycle', %s, 'CHILDRENS_HOME')""",
                (
                    actor,
                    Jsonb(
                        {
                            "policy_version": CARE_LIFECYCLE_POLICY_VERSION,
                            "limit": bounded_limit,
                            "examined": result["examined"],
                            "persisted": result["persisted"],
                            "failed": result["failed"],
                            "remaining_unset": int(remaining),
                        }
                    ),
                ),
            )
            conn.commit()
    return {
        "preview": preview,
        "policy_version": CARE_LIFECYCLE_POLICY_VERSION,
        "batch_limit": bounded_limit,
        "before_unset": int(before[0] or 0),
        "already_populated_total": int(before[1] or 0),
        **result,
        "remaining_unset": int(remaining or 0),
        "provider_requests": 0,
        "planning_watches_created": 0,
        "publication_changes": 0,
        "withdrawal_changes": 0,
    }


def care_opportunity_hygiene_audit(
    settings: Settings,
    *,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    root_cause: str | None = None,
    change_type: str | None = None,
    publication_status: str | None = None,
    q: str | None = None,
    view: str = "inventory",
) -> dict[str, Any]:
    """Audit the complete CareProspect opportunity inventory without mutation."""
    bounded_limit = min(max(int(limit), 1), 250)
    bounded_offset = max(int(offset), 0)
    if category is not None and category not in HYGIENE_CATEGORIES:
        raise ValueError("invalid_hygiene_category")
    if root_cause is not None and root_cause not in ORPHAN_ROOT_CAUSES:
        raise ValueError("invalid_hygiene_root_cause")
    if view not in {"inventory", "needs_attention", "publication_candidates"}:
        raise ValueError("invalid_hygiene_view")
    normalized_change_type = str(change_type or "").strip().upper() or None
    normalized_publication = str(publication_status or "").strip().upper() or None
    search = str(q or "").strip().lower()[:200]
    with connection(settings) as conn:
        rows = conn.execute(
            """
            SELECT o.id, o.nursery_id, o.operator_id, o.name, o.event_type,
                   o.lifecycle_stage, o.expected_opening_date, o.capacity, o.confidence,
                   o.review_status, o.publication_status, o.first_seen_at,
                   o.latest_update_at, o.created_at, o.updated_at, o.vertical,
                   o.operator_name, o.address, o.postcode, o.town,
                   o.merged_into_opportunity_id, o.change_type, o.confidence_breakdown,
                   o.stage_reason, o.creation_reason, o.customer_title,
                   o.customer_summary, o.customer_published_by, o.customer_published_at,
                   o.customer_withdrawn_at, o.customer_lifecycle_stage,
                   o.publication_automation_blocked,
                   o.publication_automation_provenance,
                   COALESCE(rel.relationships, '[]'::jsonb),
                   COALESCE(hist.actions, ARRAY[]::text[]),
                   COALESCE(audit.actions, ARRAY[]::text[]),
                   COALESCE(matches.pending_count, 0),
                   COALESCE(saved.saved_count, 0),
                   COALESCE(watches.enabled_count, 0)
            FROM opportunities o
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'signal_id', os.raw_signal_id,
                    'status', os.status,
                    'relationship_type', os.relationship_type,
                    'created_by', os.created_by,
                    'match_outcome', os.match_outcome,
                    'match_confidence', os.match_confidence,
                    'match_reason', os.match_reason,
                    'admin_override_by', os.admin_override_by,
                    'provenance', os.provenance,
                    'source_type', rs.source_type,
                    'external_id', rs.external_id,
                    'source_url', rs.source_url,
                    'title', rs.title,
                    'metadata', rs.metadata,
                    'review_status', se.review_status,
                    'extracted_facts', se.extracted_facts,
                    'relationship_extracted_facts', os.extracted_facts,
                    'planning_family_relationship_types', COALESCE(
                        (SELECT jsonb_agg(DISTINCT family_rel.relationship_type)
                         FROM planning_signal_family_relationships family_rel
                         WHERE family_rel.raw_signal_id = rs.id),
                        '[]'::jsonb
                    )
                ) ORDER BY rs.discovered_at, rs.id) AS relationships
                FROM opportunity_signals os
                JOIN raw_signals rs ON rs.id = os.raw_signal_id
                LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
                WHERE os.opportunity_id = o.id
            ) rel ON TRUE
            LEFT JOIN LATERAL (
                SELECT array_agg(DISTINCT h.action) AS actions
                FROM opportunity_signal_history h WHERE h.opportunity_id = o.id
            ) hist ON TRUE
            LEFT JOIN LATERAL (
                SELECT array_agg(DISTINCT a.action) AS actions
                FROM admin_audit_events a
                WHERE a.target_type IN ('opportunity', 'opportunity_match')
                  AND (
                    a.details->>'opportunity_id' = o.id::text
                    OR a.details->>'source_opportunity_id' = o.id::text
                    OR a.details->>'target_opportunity_id' = o.id::text
                    OR a.details->>'new_opportunity_id' = o.id::text
                  )
            ) audit ON TRUE
            LEFT JOIN LATERAL (
                SELECT count(*) AS pending_count FROM opportunity_match_reviews mr
                WHERE mr.opportunity_id = o.id AND mr.status = 'PENDING'
            ) matches ON TRUE
            LEFT JOIN LATERAL (
                SELECT count(*) AS saved_count FROM customer_saved_opportunities saved
                WHERE saved.opportunity_id = o.id
            ) saved ON TRUE
            LEFT JOIN LATERAL (
                SELECT count(*) AS enabled_count FROM planning_lifecycle_watches watch
                WHERE watch.opportunity_id = o.id AND watch.enabled
            ) watches ON TRUE
            WHERE o.vertical = 'CHILDRENS_HOME'
            ORDER BY o.created_at, o.id
            LIMIT 5000
            """
        ).fetchall()
    fields = (
        "id",
        "nursery_id",
        "operator_id",
        "name",
        "event_type",
        "lifecycle_stage",
        "expected_opening_date",
        "capacity",
        "confidence",
        "review_status",
        "publication_status",
        "first_seen_at",
        "latest_update_at",
        "created_at",
        "updated_at",
        "vertical",
        "operator_name",
        "address",
        "postcode",
        "town",
        "merged_into_opportunity_id",
        "change_type",
        "confidence_breakdown",
        "stage_reason",
        "creation_reason",
        "customer_title",
        "customer_summary",
        "customer_published_by",
        "customer_published_at",
        "customer_withdrawn_at",
        "customer_lifecycle_stage",
        "publication_automation_blocked",
        "publication_automation_provenance",
        "relationships",
        "history_actions",
        "audit_actions",
        "pending_match_reviews",
        "customer_saved_count",
        "enabled_planning_watches",
    )
    opportunities = [dict(zip(fields, row)) for row in rows]
    legacy_report = audit_opportunities(opportunities, legacy_support_semantics=True)
    report = audit_opportunities(opportunities)
    legacy_items_by_id = {item["opportunity_id"]: item for item in legacy_report["items"]}
    corrected_items_by_id = {item["opportunity_id"]: item for item in report["items"]}
    zero_foundation_with_planning = 0
    newly_foundational = 0
    no_longer_foundational = 0
    published_newly_foundational = 0
    breakdowns: dict[str, Counter[str]] = {
        "planning_subtype": Counter(),
        "review_status": Counter(),
        "opportunity_creation_decision": Counter(),
        "opportunity_change_type": Counter(),
        "legacy_hygiene_category": Counter(),
        "publication_status": Counter(),
    }
    changed_examples: list[dict[str, Any]] = []
    named_examples: list[dict[str, Any]] = []
    semantic_drift: list[dict[str, Any]] = []
    example_terms = {
        "LAMBOURNE": "Lambourne Close",
        "CASTLEDENE": "Castledene",
        "COCKINGTON": "Cockington Road",
        "MOSS ROAD": "Moss Road",
        "BUTLER STREET": "Butler Street",
        "ARMITAGE": "Armitage Close",
        "CROSTON": "Croston Road",
    }
    for opportunity in opportunities:
        opportunity_id = str(opportunity["id"])
        legacy_item = legacy_items_by_id[opportunity_id]
        corrected_item = corrected_items_by_id[opportunity_id]
        active_planning = [
            relation
            for relation in opportunity.get("relationships") or []
            if relation.get("status") == "ACTIVE"
            and str(relation.get("source_type") or "").lower() == "planning"
        ]
        active_relationships = [
            relation
            for relation in opportunity.get("relationships") or []
            if relation.get("status") == "ACTIVE"
        ]
        detail_before_foundational = sum(
            str(relation.get("source_type") or "").lower() != "planning"
            or is_foundational_planning_signal(
                (relation.get("extracted_facts") or {}).get("planning_subtype"),
                relation.get("metadata"),
            )
            for relation in active_relationships
        )
        if active_planning and detail_before_foundational == 0:
            zero_foundation_with_planning += 1
            breakdowns["opportunity_change_type"][
                str(opportunity.get("change_type") or "UNKNOWN")
            ] += 1
            breakdowns["legacy_hygiene_category"][legacy_item["category"]] += 1
            breakdowns["publication_status"][
                str(opportunity.get("publication_status") or "UNKNOWN")
            ] += 1
            for relation in active_planning:
                facts = relation.get("extracted_facts") or {}
                breakdowns["planning_subtype"][str(facts.get("planning_subtype") or "UNKNOWN")] += 1
                breakdowns["review_status"][str(relation.get("review_status") or "UNKNOWN")] += 1
                breakdowns["opportunity_creation_decision"][
                    str(facts.get("opportunity_creation_decision") or "UNKNOWN")
                ] += 1
        if corrected_item["foundational_signal_count"] > detail_before_foundational:
            newly_foundational += 1
            published_newly_foundational += int(
                opportunity.get("publication_status") == "PUBLISHED"
            )
            if len(changed_examples) < 100:
                changed_examples.append(
                    {
                        "opportunity_id": opportunity_id,
                        "name": opportunity.get("name"),
                        "publication_status": opportunity.get("publication_status"),
                        "change_type": opportunity.get("change_type"),
                        "before_foundational": detail_before_foundational,
                        "after_foundational": corrected_item["foundational_signal_count"],
                        "before_category": legacy_item["category"],
                        "after_category": corrected_item["category"],
                    }
                )
        elif corrected_item["foundational_signal_count"] < detail_before_foundational:
            no_longer_foundational += 1
            if len(changed_examples) < 100:
                changed_examples.append(
                    {
                        "opportunity_id": opportunity_id,
                        "name": opportunity.get("name"),
                        "publication_status": opportunity.get("publication_status"),
                        "change_type": opportunity.get("change_type"),
                        "before_foundational": detail_before_foundational,
                        "after_foundational": corrected_item["foundational_signal_count"],
                        "before_category": legacy_item["category"],
                        "after_category": corrected_item["category"],
                    }
                )
        searchable = " ".join(
            [
                str(opportunity.get("name") or ""),
                str(opportunity.get("address") or ""),
                *[str(relation.get("title") or "") for relation in active_planning],
            ]
        ).upper()
        for term, label in example_terms.items():
            if term in searchable:
                named_examples.append(
                    {
                        "label": label,
                        "opportunity_id": opportunity_id,
                        "before_foundational": detail_before_foundational,
                        "after_foundational": corrected_item["foundational_signal_count"],
                        "supporting_followups": corrected_item["supporting_followup_count"],
                        "category": corrected_item["category"],
                        "planning_signals": [
                            {
                                "signal_id": str(relation.get("signal_id")),
                                "relationship_status": relation.get("status"),
                                "review_status": relation.get("review_status"),
                                "planning_subtype": (relation.get("extracted_facts") or {}).get(
                                    "planning_subtype"
                                ),
                                "opportunity_creation_decision": (
                                    relation.get("extracted_facts") or {}
                                ).get("opportunity_creation_decision"),
                                "planning_outcome": canonical_planning_outcome(
                                    relation.get("metadata")
                                ).outcome.value,
                                "support_state": classify_evidence_support(relation).value,
                                "relationship_planning_subtype": (
                                    relation.get("relationship_extracted_facts") or {}
                                ).get("planning_subtype"),
                                "relationship_opportunity_creation_decision": (
                                    relation.get("relationship_extracted_facts") or {}
                                ).get("opportunity_creation_decision"),
                                "match_reason": relation.get("match_reason"),
                                "relationship_created_by": relation.get("created_by"),
                            }
                            for relation in active_planning
                        ],
                    }
                )
                break
        for relation in active_planning:
            facts = relation.get("extracted_facts") or {}
            subtype = str(facts.get("planning_subtype") or "")
            opportunity_change = str(opportunity.get("change_type") or "")
            drift_reason = None
            if subtype == "EXPANSION_OR_CAPACITY_CHANGE" and opportunity_change == "OPENING":
                drift_reason = "EXPANSION_SIGNAL_ON_OPENING"
            elif (
                subtype == "CESSATION_OR_CHANGE_AWAY_FROM_CARE" and opportunity_change == "OPENING"
            ):
                drift_reason = "CESSATION_SIGNAL_ON_OPENING"
            elif subtype == "LAWFULNESS_EXISTING" and opportunity_change == "OPENING":
                drift_reason = "EXISTING_LAWFULNESS_ON_OPENING"
            elif subtype == "NEW_HOME_MIXED_USE":
                drift_reason = "MIXED_USE_REVIEW"
            if drift_reason and len(semantic_drift) < 250:
                semantic_drift.append(
                    {
                        "opportunity_id": opportunity_id,
                        "signal_id": str(relation.get("signal_id")),
                        "reason": drift_reason,
                        "review_status": relation.get("review_status"),
                        "opportunity_creation_decision": facts.get("opportunity_creation_decision"),
                        "opportunity_change_type": opportunity_change,
                        "planning_subtype": subtype,
                    }
                )
    report["support_semantics_diagnostic"] = {
        "active_planning_zero_foundational_before": zero_foundation_with_planning,
        "opportunities_newly_foundational": newly_foundational,
        "opportunities_no_longer_foundational": no_longer_foundational,
        "published_newly_foundational": published_newly_foundational,
        "before_category_counts": legacy_report["category_counts"],
        "after_category_counts": report["category_counts"],
        "category_changes": Counter(
            f"{legacy_items_by_id[item_id]['category']} -> "
            f"{corrected_items_by_id[item_id]['category']}"
            for item_id in corrected_items_by_id
            if legacy_items_by_id[item_id]["category"] != corrected_items_by_id[item_id]["category"]
        ),
        "zero_foundation_breakdowns": {
            key: dict(sorted(values.items())) for key, values in breakdowns.items()
        },
        "changed_examples": changed_examples,
        "named_example_verification": named_examples,
        "semantic_drift": {
            "count": len(semantic_drift),
            "reason_counts": dict(
                sorted(Counter(item["reason"] for item in semantic_drift).items())
            ),
            "items": semantic_drift,
            "truncated": len(semantic_drift) >= 250,
        },
    }
    all_items = report.pop("items")
    selected = filter_hygiene_items(
        all_items,
        category=category,
        root_cause=root_cause,
        change_type=normalized_change_type,
        publication_status=normalized_publication,
        q=search,
        view=view,
    )
    report.update(
        {
            "items": selected[bounded_offset : bounded_offset + bounded_limit],
            "filtered_total": len(selected),
            "limit": bounded_limit,
            "offset": bounded_offset,
            "category_filter": category,
            "root_cause_filter": root_cause,
            "change_type_filter": normalized_change_type,
            "publication_status_filter": normalized_publication,
            "search": search,
            "view": view,
            "errors": 0,
            "inventory_complete": len(rows) < 5000,
            "prior_count_reconciliation": {
                "cleanup_specific": (
                    "Draft opportunities linked to the bounded refused-signal cleanup batch "
                    "with no other active non-rejected support."
                ),
                "broad_followup_only": (
                    "All draft opportunities whose active evidence included follow-up/negative "
                    "Planning subtypes and no active non-follow-up, non-rejected signal."
                ),
                "directly_comparable": False,
            },
        }
    )
    return report


def care_opportunity_orphan_cleanup(
    settings: Settings,
    *,
    apply: bool = False,
    actor: str = "iam-care-opportunity-orphan-cleanup",
    limit: int = 25,
) -> dict[str, Any]:
    """Preview or terminally resolve a bounded set of safe unsupported shells."""
    bounded_limit = min(max(int(limit), 1), 50)
    before = care_opportunity_hygiene_audit(
        settings,
        limit=250,
        category="UNSUPPORTED_ORPHAN_CANDIDATE",
        view="inventory",
    )
    attention_before = care_opportunity_hygiene_audit(settings, limit=1, view="needs_attention")
    # The authoritative audit can exceed one page; its orphan_candidates list is complete.
    cohort = before["orphan_candidates"]
    summary = summarize_orphan_cleanup(cohort)
    eligible = [
        decision for decision in summary["decisions"] if decision["outcome"] == AUTO_RESOLVE
    ]
    restore_candidates = [
        item
        for item in cohort
        if item.get("review_status") == "REJECTED"
        and item.get("system_cleanup_resolved")
        and str(item.get("customer_lifecycle_stage") or "") not in {"STOPPED", "NEEDS_REVIEW"}
    ][:bounded_limit]
    selected = eligible[: max(bounded_limit - len(restore_candidates), 0)]
    resolved = 0
    restored_for_safety = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    applied_ids: list[str] = []

    if apply:
        for item in restore_candidates:
            opportunity_id = str(UUID(item["opportunity_id"]))
            try:
                with connection(settings) as conn:
                    row = conn.execute(
                        """SELECT o.review_status, o.stage_reason,
                                  o.customer_lifecycle_stage, audit.details
                           FROM opportunities o
                           JOIN LATERAL (
                               SELECT details FROM admin_audit_events
                               WHERE action = 'opportunity_unsupported_orphan_resolved'
                                 AND details->>'opportunity_id' = o.id::text
                               ORDER BY created_at DESC LIMIT 1
                           ) audit ON TRUE
                           WHERE o.id = %s AND o.vertical = 'CHILDRENS_HOME'
                           FOR UPDATE OF o""",
                        (opportunity_id,),
                    ).fetchone()
                    if (
                        row is None
                        or row[0] != "REJECTED"
                        or str(row[2] or "") in {"STOPPED", "NEEDS_REVIEW"}
                    ):
                        skipped += 1
                        conn.rollback()
                        continue
                    details = row[3] or {}
                    old_status = str(details.get("old_review_status") or "UNREVIEWED")
                    if old_status not in {"UNREVIEWED", "IN_REVIEW", "APPROVED"}:
                        old_status = "UNREVIEWED"
                    old_stage_reason = details.get("old_stage_reason")
                    conn.execute(
                        """UPDATE opportunities SET review_status = %s, stage_reason = %s,
                                  updated_at = now()
                           WHERE id = %s AND review_status = 'REJECTED'""",
                        (old_status, old_stage_reason, opportunity_id),
                    )
                    conn.execute(
                        """INSERT INTO admin_audit_events
                           (action, actor, target_type, target_count, details)
                           VALUES ('opportunity_unsupported_orphan_resolution_reverted', %s,
                                   'opportunity', 1, %s)""",
                        (
                            actor,
                            Jsonb(
                                {
                                    "opportunity_id": opportunity_id,
                                    "policy_version": OPPORTUNITY_ORPHAN_CLEANUP_POLICY_VERSION,
                                    "reason": "non_terminal_customer_lifecycle",
                                    "customer_lifecycle_stage": row[2],
                                    "restored_review_status": old_status,
                                }
                            ),
                        ),
                    )
                    conn.commit()
                    restored_for_safety += 1
            except Exception as exc:
                failures.append({"opportunity_id": opportunity_id, "error": type(exc).__name__})
        for decision in selected:
            opportunity_id = str(UUID(decision["opportunity_id"]))
            try:
                with connection(settings) as conn:
                    row = conn.execute(
                        """SELECT review_status, publication_status, customer_published_at,
                                  customer_withdrawn_at, customer_title, customer_summary,
                                  publication_automation_blocked,
                                  publication_automation_provenance, stage_reason
                           FROM opportunities
                           WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                           FOR UPDATE""",
                        (opportunity_id,),
                    ).fetchone()
                    unsafe = (
                        row is None
                        or row[0] in {"REJECTED", "MERGED"}
                        or row[1] != "DRAFT"
                        or any(row[index] for index in (2, 3, 4, 5, 6, 7))
                        or conn.execute(
                            """SELECT EXISTS (
                                   SELECT 1 FROM customer_saved_opportunities
                                   WHERE opportunity_id = %s
                               ) OR EXISTS (
                                   SELECT 1 FROM planning_lifecycle_watches
                                   WHERE opportunity_id = %s AND enabled
                               ) OR EXISTS (
                                   SELECT 1 FROM opportunity_match_reviews
                                   WHERE opportunity_id = %s AND status = 'PENDING'
                               ) OR EXISTS (
                                   SELECT 1 FROM opportunity_signal_history
                                   WHERE opportunity_id = %s AND action LIKE 'ADMIN%%'
                               ) OR EXISTS (
                                   SELECT 1 FROM opportunity_signals
                                   WHERE opportunity_id = %s
                                     AND (created_by <> 'SYSTEM' OR admin_override_by IS NOT NULL)
                               )""",
                            (
                                opportunity_id,
                                opportunity_id,
                                opportunity_id,
                                opportunity_id,
                                opportunity_id,
                            ),
                        ).fetchone()[0]
                    )
                    if unsafe:
                        skipped += 1
                        conn.rollback()
                        continue
                    old_stage_reason = row[8]
                    stage_reason = (
                        "Automatically resolved unsupported opportunity shell; "
                        f"{decision['root_cause']}."
                    )
                    updated = conn.execute(
                        """UPDATE opportunities
                           SET review_status = 'REJECTED', stage_reason = %s, updated_at = now()
                           WHERE id = %s AND review_status NOT IN ('REJECTED', 'MERGED')
                           RETURNING id""",
                        (stage_reason, opportunity_id),
                    ).fetchone()
                    if not updated:
                        skipped += 1
                        conn.rollback()
                        continue
                    conn.execute(
                        """INSERT INTO admin_audit_events
                           (action, actor, target_type, target_count, details)
                           VALUES ('opportunity_unsupported_orphan_resolved', %s,
                                   'opportunity', 1, %s)""",
                        (
                            actor,
                            Jsonb(
                                {
                                    "opportunity_id": opportunity_id,
                                    "policy_version": OPPORTUNITY_ORPHAN_CLEANUP_POLICY_VERSION,
                                    "root_cause": decision["root_cause"],
                                    "old_review_status": row[0],
                                    "new_review_status": "REJECTED",
                                    "old_stage_reason": old_stage_reason,
                                    "new_stage_reason": stage_reason,
                                    "foundational_signal_count": decision[
                                        "foundational_signal_count"
                                    ],
                                    "supporting_followup_count": decision[
                                        "supporting_followup_count"
                                    ],
                                    "active_relationships_preserved": decision[
                                        "active_relationships"
                                    ],
                                }
                            ),
                        ),
                    )
                    conn.commit()
                    resolved += 1
                    applied_ids.append(opportunity_id)
            except Exception as exc:
                failures.append(
                    {
                        "opportunity_id": opportunity_id,
                        "error": type(exc).__name__,
                    }
                )

    after = (
        care_opportunity_hygiene_audit(settings, limit=1, view="needs_attention") if apply else None
    )
    return {
        "policy_version": OPPORTUNITY_ORPHAN_CLEANUP_POLICY_VERSION,
        "apply": apply,
        "batch_limit": bounded_limit,
        "candidate_count": summary["candidate_count"],
        "root_cause_counts": summary["root_cause_counts"],
        "auto_resolvable_count": len(eligible),
        "preserved_manual_review_count": summary["outcome_counts"].get("PRESERVE", 0),
        "already_resolved_count": summary["outcome_counts"].get(ALREADY_RESOLVED, 0),
        "preservation_reason_counts": summary["preservation_reason_counts"],
        "selected": len(selected),
        "resolved": resolved,
        "safety_restore_candidates": len(restore_candidates),
        "restored_for_safety": restored_for_safety,
        "skipped": skipped,
        "failed": len(failures),
        "failures": failures,
        "applied_opportunity_ids": applied_ids,
        "sample_auto_resolvable": eligible[:10],
        "sample_preserved": [
            decision for decision in summary["decisions"] if decision["outcome"] == "PRESERVE"
        ][:10],
        "needs_attention_before": attention_before.get("filtered_total"),
        "needs_attention_after": after.get("filtered_total") if after else None,
        "publication_changes": 0,
        "withdrawal_changes": 0,
        "relationship_changes": 0,
        "provider_requests": 0,
    }


def _care_semantic_drift_inventory(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT o.id, o.name, o.event_type, o.lifecycle_stage, o.review_status,
               o.publication_status, o.vertical, o.change_type, o.stage_reason,
               o.creation_reason, o.updated_at,
               COALESCE(rel.relationships, '[]'::jsonb),
               COALESCE(hist.actions, ARRAY[]::text[]),
               COALESCE(audit.actions, ARRAY[]::text[])
        FROM opportunities o
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(jsonb_build_object(
                'signal_id', os.raw_signal_id,
                'status', os.status,
                'relationship_type', os.relationship_type,
                'created_by', os.created_by,
                'admin_override_by', os.admin_override_by,
                'source_type', rs.source_type,
                'title', rs.title,
                'raw_text', rs.raw_text,
                'metadata', rs.metadata,
                'review_status', se.review_status,
                'extracted_facts', se.extracted_facts,
                'relationship_extracted_facts', os.extracted_facts,
                'planning_family_relationship_types', COALESCE(
                    (SELECT jsonb_agg(DISTINCT family_rel.relationship_type)
                     FROM planning_signal_family_relationships family_rel
                     WHERE family_rel.raw_signal_id = rs.id),
                    '[]'::jsonb
                )
            ) ORDER BY rs.discovered_at, rs.id) AS relationships
            FROM opportunity_signals os
            JOIN raw_signals rs ON rs.id = os.raw_signal_id
            LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
            WHERE os.opportunity_id = o.id
        ) rel ON TRUE
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT h.action) AS actions
            FROM opportunity_signal_history h WHERE h.opportunity_id = o.id
        ) hist ON TRUE
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT a.action) AS actions
            FROM admin_audit_events a
            WHERE a.target_type IN ('opportunity', 'opportunity_match')
              AND (
                a.details->>'opportunity_id' = o.id::text
                OR a.details->>'source_opportunity_id' = o.id::text
                OR a.details->>'target_opportunity_id' = o.id::text
                OR a.details->>'new_opportunity_id' = o.id::text
              )
        ) audit ON TRUE
        WHERE o.vertical = 'CHILDRENS_HOME' AND o.change_type = 'OPENING'
        ORDER BY o.created_at, o.id
        LIMIT 5000
        """
    ).fetchall()
    fields = (
        "id",
        "name",
        "event_type",
        "lifecycle_stage",
        "review_status",
        "publication_status",
        "vertical",
        "change_type",
        "stage_reason",
        "creation_reason",
        "updated_at",
        "relationships",
        "history_actions",
        "audit_actions",
    )
    opportunities = [dict(zip(fields, row)) for row in rows]
    for opportunity in opportunities:
        opportunity["admin_touch_types"] = opportunity_admin_touch_types(opportunity)
        for relation in opportunity["relationships"]:
            if str(relation.get("source_type") or "").lower() != "planning":
                continue
            assessment = classify_care_planning_subtype(
                {
                    "title": relation.get("title"),
                    "raw_text": relation.get("raw_text"),
                    "metadata": relation.get("metadata"),
                }
            )
            relation["recomputed_planning_subtype"] = assessment.subtype
    return opportunities


def care_opportunity_semantic_drift_cleanup(
    settings: Settings,
    *,
    apply: bool = False,
    actor: str = "iam-care-opportunity-semantic-drift",
    limit: int = 25,
) -> dict[str, Any]:
    """Preview or apply the narrow audited CareProspect semantic-drift correction."""
    bounded_limit = min(max(int(limit), 1), 25)
    hygiene_before = care_opportunity_hygiene_audit(settings, limit=1)
    with connection(settings) as conn:
        opportunities = _care_semantic_drift_inventory(conn)
    plans = [
        plan
        for opportunity in opportunities
        if (plan := plan_opportunity_semantic_drift(opportunity)) is not None
    ]
    safe_plans = [plan for plan in plans if plan["safe_to_apply"]]
    result: dict[str, Any] = {
        "policy_version": OPPORTUNITY_SEMANTIC_DRIFT_POLICY_VERSION,
        "apply": apply,
        "opening_opportunities_inspected": len(opportunities),
        "drift_candidates": len(plans),
        "drift_type_counts": dict(
            sorted(Counter(reason for plan in plans for reason in plan["drift_types"]).items())
        ),
        "published_candidates": sum(
            plan["current"]["publication_status"] == "PUBLISHED" for plan in plans
        ),
        "admin_touched_candidates": sum(bool(plan["admin_touch_types"]) for plan in plans),
        "safe_to_apply": len(safe_plans),
        "manual_review": len(plans) - len(safe_plans),
        "items": plans,
        "selected": 0,
        "corrected": 0,
        "skipped": 0,
        "errors": 0,
        "audit_event_ids": [],
        "hygiene_before": {
            "category_counts": hygiene_before["category_counts"],
            "customer_readiness_total": hygiene_before["customer_readiness_total"],
            "publication_counts": hygiene_before["publication_counts"],
        },
    }
    if apply and safe_plans:
        selected = safe_plans[:bounded_limit]
        result["selected"] = len(selected)
        selected_ids = [UUID(plan["opportunity_id"]) for plan in selected]
        with connection(settings) as conn:
            conn.execute(
                "SELECT id FROM opportunities WHERE id = ANY(%s) FOR UPDATE",
                (selected_ids,),
            ).fetchall()
            conn.execute(
                """SELECT se.raw_signal_id
                   FROM signal_enrichments se
                   JOIN opportunity_signals os ON os.raw_signal_id = se.raw_signal_id
                   WHERE os.opportunity_id = ANY(%s) FOR UPDATE""",
                (selected_ids,),
            ).fetchall()
            current_by_id = {str(item["id"]): item for item in _care_semantic_drift_inventory(conn)}
            for original in selected:
                opportunity_id = original["opportunity_id"]
                current = current_by_id.get(opportunity_id)
                current_plan = (
                    plan_opportunity_semantic_drift(current) if current is not None else None
                )
                if not current_plan or not current_plan["safe_to_apply"]:
                    result["skipped"] += 1
                    continue
                proposed = current_plan["proposed"]
                updated = conn.execute(
                    """UPDATE opportunities
                       SET name = %s, change_type = %s, event_type = %s,
                           stage_reason = %s, latest_update_at = now(), updated_at = now()
                       WHERE id = %s AND vertical = 'CHILDRENS_HOME'
                         AND change_type = 'OPENING' AND publication_status = 'DRAFT'
                       RETURNING id""",
                    (
                        proposed["name"],
                        proposed["change_type"],
                        proposed["event_type"],
                        proposed["stage_reason"],
                        opportunity_id,
                    ),
                ).fetchone()
                if not updated:
                    result["skipped"] += 1
                    continue
                audit = conn.execute(
                    """INSERT INTO admin_audit_events
                       (action, actor, target_type, details)
                       VALUES ('opportunity_semantic_drift_corrected', %s,
                               'opportunity', %s) RETURNING id""",
                    (
                        actor[:200],
                        Jsonb(
                            {
                                "opportunity_id": opportunity_id,
                                "policy_version": OPPORTUNITY_SEMANTIC_DRIFT_POLICY_VERSION,
                                "drift_types": current_plan["drift_types"],
                                "signal_ids": current_plan["drift_signal_ids"],
                                "before": current_plan["current"],
                                "after": proposed,
                                "creation_reason_preserved": True,
                                "publication_unchanged": True,
                            }
                        ),
                    ),
                ).fetchone()
                result["audit_event_ids"].append(str(audit[0]))
                result["corrected"] += 1
            conn.commit()

    hygiene_after = care_opportunity_hygiene_audit(settings, limit=1)
    result["hygiene_after"] = {
        "category_counts": hygiene_after["category_counts"],
        "customer_readiness_total": hygiene_after["customer_readiness_total"],
        "publication_counts": hygiene_after["publication_counts"],
    }
    result["bounded_limit"] = bounded_limit
    return result


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
                       op.companies_house_url, op.resolution_outcome,
                       op.organisation_type, op.organisation_type_source
                FROM grouped g
                LEFT JOIN operators op
                  ON lower(trim(op.name)) = lower(trim(g.name))
                  OR lower(trim(op.legal_name)) = lower(trim(g.name))
                  OR EXISTS (
                    SELECT 1 FROM organisation_aliases oa
                    WHERE oa.operator_id = op.id
                      AND lower(trim(oa.alias)) = lower(trim(g.name))
                  )
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
                "organisation_type": row[11] or UNKNOWN,
                "organisation_type_source": row[12],
                "public_authority_suspected": bool(
                    is_public_authority_name(row[0]) and (row[11] or UNKNOWN) != PUBLIC_AUTHORITY
                ),
                "public_authority_conflict": bool(
                    (row[11] or UNKNOWN) == PUBLIC_AUTHORITY and row[6]
                ),
            }
            for row in rows
        ],
        "limit": limit,
        "offset": offset,
    }


def public_authority_backfill(
    settings: Settings, *, apply: bool = False, actor: str = "SYSTEM", limit: int = 5000
) -> dict[str, Any]:
    """Preview or safely classify obvious public authorities without replacing identities."""
    bounded_limit = min(max(int(limit), 1), 5000)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT o.id, o.name, o.legal_name, o.companies_house_number,
                      o.organisation_type, o.organisation_type_source,
                      o.enrichment_provenance,
                      (SELECT count(*) FROM opportunities opp
                       WHERE opp.operator_id = o.id) AS opportunity_count,
                      (SELECT count(DISTINCT linked.signal_id) FROM (
                         SELECT os.raw_signal_id AS signal_id
                         FROM opportunities opp
                         JOIN opportunity_signals os ON os.opportunity_id = opp.id
                         WHERE opp.operator_id = o.id
                         UNION
                         SELECT oe.raw_signal_id AS signal_id
                         FROM ofsted_urn_enrichments oe WHERE oe.operator_id = o.id
                       ) linked) AS signal_count,
                      (SELECT count(*) FROM organisation_match_reviews r
                       WHERE r.operator_id = o.id AND r.provider = 'COMPANIES_HOUSE'
                         AND r.status = 'PENDING') AS pending_review_count,
                      EXISTS (
                        SELECT 1 FROM organisation_match_reviews r
                        WHERE r.operator_id = o.id AND r.status = 'CONFIRMED'
                          AND COALESCE(r.reviewed_by, 'SYSTEM') <> 'SYSTEM'
                      ) OR COALESCE(o.enrichment_provenance->>'selection_source', '') IN
                          ('MANUAL_COMPANY_NUMBER', 'SUGGESTED_CANDIDATE') AS manual_mapping
               FROM operators o ORDER BY o.created_at, o.id LIMIT %s""",
            (bounded_limit,),
        ).fetchall()
        items = []
        counts = {
            "organisations_inspected": len(rows),
            "likely_public_authority": 0,
            "no_ch_mapping": 0,
            "pending_ch_review": 0,
            "system_mapped_ch": 0,
            "manually_confirmed_ch": 0,
            "ambiguous_needs_review": 0,
            "already_public_authority": 0,
        }
        for row in rows:
            operator_id = str(row[0])
            name = str(row[1] or row[2] or "").strip()
            company_number = str(row[3] or "").strip() or None
            organisation_type = str(row[4] or UNKNOWN)
            obvious = is_public_authority_name(name) or is_public_authority_name(row[2])
            ambiguous = not obvious and (
                looks_like_public_authority(name) or looks_like_public_authority(row[2])
            )
            if ambiguous:
                counts["ambiguous_needs_review"] += 1
            if not obvious:
                continue
            counts["likely_public_authority"] += 1
            if organisation_type == PUBLIC_AUTHORITY:
                counts["already_public_authority"] += 1
            if not company_number:
                counts["no_ch_mapping"] += 1
                mapping_state = "NO_MAPPING"
            elif row[10]:
                counts["manually_confirmed_ch"] += 1
                mapping_state = "MANUAL_CONFLICT"
            else:
                counts["system_mapped_ch"] += 1
                mapping_state = "SYSTEM_MAPPING"
            if row[9]:
                counts["pending_ch_review"] += 1
            eligible = not row[10]
            items.append(
                {
                    "operator_id": operator_id,
                    "name": name,
                    "organisation_type": organisation_type,
                    "companies_house_number": company_number,
                    "mapping_state": mapping_state,
                    "pending_review_count": int(row[9] or 0),
                    "linked_signal_count": int(row[8] or 0),
                    "linked_opportunity_count": int(row[7] or 0),
                    "manual_mapping": bool(row[10]),
                    "safe_to_correct": eligible,
                }
            )
        result: dict[str, Any] = {
            "mode": "APPLY" if apply else "PREVIEW",
            "counts": counts,
            "items": items,
            "classified": 0,
            "reviews_closed": 0,
            "system_mappings_removed": 0,
            "manual_conflicts_preserved": sum(
                1 for item in items if item["mapping_state"] == "MANUAL_CONFLICT"
            ),
            "errors": 0,
        }
        if not apply:
            return result
        for item in items:
            if not item["safe_to_correct"]:
                continue
            operator_id = item["operator_id"]
            current = conn.execute(
                """SELECT organisation_type, companies_house_number
                   FROM operators WHERE id = %s FOR UPDATE""",
                (operator_id,),
            ).fetchone()
            if not current:
                result["errors"] += 1
                continue
            changed = current[0] != PUBLIC_AUTHORITY
            remove_system_mapping = bool(current[1])
            if changed or remove_system_mapping:
                conn.execute(
                    """UPDATE operators SET organisation_type = 'PUBLIC_AUTHORITY',
                       organisation_type_source = 'DETERMINISTIC_NAME_BACKFILL',
                       organisation_type_updated_at = now(),
                       companies_house_number = CASE WHEN %s THEN NULL
                                                    ELSE companies_house_number END,
                       company_status = CASE WHEN %s THEN NULL ELSE company_status END,
                       incorporation_date = CASE WHEN %s THEN NULL ELSE incorporation_date END,
                       company_type = CASE WHEN %s THEN NULL ELSE company_type END,
                       registered_office = CASE WHEN %s THEN '{}'::jsonb
                                                ELSE registered_office END,
                       sic_codes = CASE WHEN %s THEN '[]'::jsonb ELSE sic_codes END,
                       companies_house_url = CASE WHEN %s THEN NULL
                                                  ELSE companies_house_url END,
                       companies_house_refreshed_at = CASE WHEN %s THEN NULL
                         ELSE companies_house_refreshed_at END,
                       resolution_outcome = CASE WHEN %s THEN NULL
                                                 ELSE resolution_outcome END,
                       resolution_confidence = CASE WHEN %s THEN NULL
                                                    ELSE resolution_confidence END,
                       enrichment_provenance = CASE WHEN %s THEN '{}'::jsonb
                                                    ELSE enrichment_provenance END,
                       updated_at = now() WHERE id = %s""",
                    (*([remove_system_mapping] * 11), operator_id),
                )
                result["classified"] += 1
                if remove_system_mapping:
                    result["system_mappings_removed"] += 1
            closed = conn.execute(
                """UPDATE organisation_match_reviews
                   SET status = 'SUPERSEDED', reviewed_by = 'SYSTEM', reviewed_at = now()
                   WHERE operator_id = %s AND provider = 'COMPANIES_HOUSE'
                     AND status = 'PENDING' RETURNING id""",
                (operator_id,),
            ).fetchall()
            result["reviews_closed"] += len(closed)
            _store_public_authority_aliases(conn, operator_id, item["name"])
            if changed or remove_system_mapping or closed:
                conn.execute(
                    """INSERT INTO admin_audit_events
                       (action, actor, target_type, details)
                       VALUES ('organisation_public_authority_classified', %s,
                               'organisation', %s)""",
                    (
                        actor,
                        Jsonb(
                            {
                                "operator_id": operator_id,
                                "policy": "public-authority-v1",
                                "system_mapping_removed": remove_system_mapping,
                                "reviews_closed": len(closed),
                            }
                        ),
                    ),
                )
        conn.commit()
    return result


def set_organisation_type(
    settings: Settings, operator_id: str, *, organisation_type: str, actor: str
) -> dict[str, Any]:
    requested = str(organisation_type or "").strip().upper()
    if requested == "NORMAL_RESOLUTION":
        requested = PRIVATE_COMPANY
    if requested not in ORGANISATION_TYPES:
        raise ValueError("invalid organisation type")
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT name, companies_house_number, organisation_type
               FROM operators WHERE id = %s FOR UPDATE""",
            (operator_id,),
        ).fetchone()
        if not row:
            raise ValueError("organisation not found")
        effective = PRIVATE_COMPANY if requested == UNKNOWN and row[1] else requested
        if row[2] == effective:
            return {"id": operator_id, "organisation_type": effective, "idempotent": True}
        conn.execute(
            """UPDATE operators SET organisation_type = %s,
               organisation_type_source = 'ADMIN', organisation_type_updated_at = now(),
               updated_at = now() WHERE id = %s""",
            (effective, operator_id),
        )
        if effective == PUBLIC_AUTHORITY:
            _store_public_authority_aliases(conn, operator_id, row[0])
            if not row[1]:
                conn.execute(
                    """UPDATE organisation_match_reviews SET status = 'SUPERSEDED',
                       reviewed_by = %s, reviewed_at = now()
                       WHERE operator_id = %s AND provider = 'COMPANIES_HOUSE'
                         AND status = 'PENDING'""",
                    (actor, operator_id),
                )
        conn.execute(
            """INSERT INTO admin_audit_events (action, actor, target_type, details)
               VALUES ('organisation_type_changed', %s, 'organisation', %s)""",
            (
                actor,
                Jsonb(
                    {
                        "operator_id": operator_id,
                        "from": row[2],
                        "to": effective,
                        "companies_house_mapping_preserved": bool(row[1]),
                    }
                ),
            ),
        )
        conn.commit()
    return {
        "id": operator_id,
        "organisation_type": effective,
        "companies_house_mapping_preserved": bool(row[1]),
        "idempotent": False,
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
                          ), organisation_type
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
            organisation_type = operator[4] if len(operator) > 4 else UNKNOWN
            conn.execute(
                """UPDATE opportunities SET operator_id = %s, updated_at = now()
                   WHERE vertical = %s AND operator_id IS NULL
                     AND lower(trim(operator_name)) = lower(trim(%s))
                     AND review_status NOT IN ('MERGED', 'REJECTED')""",
                (operator_id, vertical, name),
            )
            if organisation_type == PUBLIC_AUTHORITY or (
                organisation_type != PRIVATE_COMPANY and is_public_authority_name(operator[0])
            ):
                if organisation_type == UNKNOWN and not operator[1]:
                    conn.execute(
                        """UPDATE operators SET organisation_type = 'PUBLIC_AUTHORITY',
                           organisation_type_source = 'DETERMINISTIC_NAME',
                           organisation_type_updated_at = now(), updated_at = now()
                           WHERE id = %s AND organisation_type = 'UNKNOWN'
                             AND companies_house_number IS NULL""",
                        (operator_id,),
                    )
                    _store_public_authority_aliases(conn, operator_id, operator[0])
                continue
            if (
                refreshed_at
                and refreshed_at > datetime.now(UTC) - timedelta(days=30)
                and (not provider_evidence_at or provider_evidence_at <= refreshed_at)
                and not (provider_name and len(operator) > 3 and operator[3])
            ):
                continue
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
                        str(alias[0]).strip() for alias in aliases if str(alias[0] or "").strip()
                    ],
                    "organisation_type": organisation_type,
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
                      resolution_confidence, enrichment_provenance,
                      organisation_type, organisation_type_source,
                      organisation_type_updated_at
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
        "id",
        "name",
        "legal_name",
        "companies_house_number",
        "website_url",
        "company_status",
        "incorporation_date",
        "company_type",
        "registered_office",
        "sic_codes",
        "companies_house_url",
        "companies_house_refreshed_at",
        "resolution_outcome",
        "resolution_confidence",
        "enrichment_provenance",
        "organisation_type",
        "organisation_type_source",
        "organisation_type_updated_at",
    )
    result = dict(zip(fields, operator))
    result["public_authority_suspected"] = bool(
        is_public_authority_name(result.get("name"))
        and result.get("organisation_type") != PUBLIC_AUTHORITY
    )
    result["public_authority_conflict"] = bool(
        result.get("organisation_type") == PUBLIC_AUTHORITY and result.get("companies_house_number")
    )
    result["aliases"] = [
        {"alias": row[0], "source": row[1], "created_at": row[2]} for row in aliases
    ]
    result["opportunities"] = [
        {
            "id": row[0],
            "name": row[1],
            "vertical": row[2],
            "change_type": row[3],
            "lifecycle_stage": row[4],
            "confidence": row[5],
        }
        for row in opportunities
    ]
    result["evidence"] = [
        {
            "provider": row[0],
            "external_id": row[1],
            "status": row[2],
            "resolution_outcome": row[3],
            "resolution_confidence": row[4],
            "reason": row[5],
            "retrieved_at": row[6],
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


def list_organisation_match_reviews(settings: Settings, *, limit: int = 25) -> list[dict[str, Any]]:
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT r.id, r.operator_id, o.name, r.provider, r.query_name,
                      r.candidates, r.reason, r.created_at
               FROM organisation_match_reviews r
               JOIN operators o ON o.id = r.operator_id
               WHERE r.status = 'PENDING'
                 AND (o.organisation_type <> 'PUBLIC_AUTHORITY'
                      OR o.companies_house_number IS NOT NULL)
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
            enriched_urns = {str(item.get("urn") or "").upper() for item in ofsted_items}
            source_urns = {
                str((signal[7] or {}).get("ofsted_urn") or "").strip().upper()
                for signal in signals
                if signal[3] == "ofsted" and str((signal[7] or {}).get("ofsted_urn") or "").strip()
            }
            candidates = row[5] or []
            if ofsted_items:
                evidence = ofsted_items[0]
                source_signal = signals[0] if signals else None
                comparison_context = OrganisationCandidate(
                    operator_id=str(row[1]),
                    name=str(row[4] or row[2]),
                    locality=(source_signal[7] or {}).get("town") if source_signal else None,
                    postcode=(source_signal[7] or {}).get("postcode") if source_signal else None,
                    provider_registered_name=evidence.get("registered_provider_name"),
                    provider_registered_locality=evidence.get("provider_registered_locality"),
                    provider_registered_postcode=evidence.get("provider_registered_postcode"),
                    provider_registered_address=evidence.get("provider_registered_address"),
                    provider_registration_date=(
                        str(evidence["registration_date"])
                        if evidence.get("registration_date")
                        else None
                    ),
                )
                candidates = rank_company_candidates(
                    [
                        {
                            **compare_company_candidate(comparison_context, candidate),
                            "selection_source": candidate.get("selection_source", "SUGGESTED"),
                        }
                        for candidate in candidates
                        if isinstance(candidate, dict)
                    ]
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
                        "aliases": [{"alias": alias[0], "source": alias[1]} for alias in aliases],
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
                                "ofsted_urn": (signal[7] or {}).get("ofsted_urn"),
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
                        "missing_ofsted_urns": sorted(source_urns - enriched_urns),
                    },
                }
            )
    return results


def request_organisation_review_ofsted_enrichment(
    settings: Settings,
    review_id: str,
    *,
    actor: str,
) -> dict[str, Any]:
    """Reserve one bounded URN enrichment request without duplicating active work."""
    review = next(
        (
            item
            for item in list_organisation_match_reviews(settings, limit=100)
            if str(item.get("id")) == review_id
        ),
        None,
    )
    if not review:
        raise ValueError("organisation review not found")
    missing = (review.get("source_context") or {}).get("missing_ofsted_urns") or []
    if not missing:
        return {"status": "ALREADY_ENRICHED", "queued": False}
    urn = sorted({str(value).upper() for value in missing})[0]
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT status, ofsted_enrichment_requested_urn,
                      ofsted_enrichment_requested_at
               FROM organisation_match_reviews
               WHERE id = %s FOR UPDATE""",
            (review_id,),
        ).fetchone()
        if not row:
            raise ValueError("organisation review not found")
        if row[0] != "PENDING":
            raise ValueError("organisation review is no longer pending")
        requested_at = row[2]
        if row[1] == urn and requested_at:
            age_seconds = (datetime.now(UTC) - requested_at).total_seconds()
            if age_seconds < 900:
                return {"status": "ALREADY_QUEUED", "queued": False, "urn": urn}
        conn.execute(
            """UPDATE organisation_match_reviews
               SET ofsted_enrichment_requested_urn = %s,
                   ofsted_enrichment_requested_at = now()
               WHERE id = %s""",
            (urn, review_id),
        )
        conn.execute(
            """INSERT INTO admin_audit_events (action, actor, target_type, details)
               VALUES ('organisation_match_ofsted_enrichment_requested', %s,
                       'organisation_match_review', %s)""",
            (actor, Jsonb({"review_id": review_id, "urn": urn})),
        )
        conn.commit()
    return {"status": "QUEUED", "queued": True, "urn": urn}


def release_organisation_review_ofsted_enrichment_request(
    settings: Settings, review_id: str, urn: str
) -> None:
    with connection(settings) as conn:
        conn.execute(
            """UPDATE organisation_match_reviews
               SET ofsted_enrichment_requested_urn = NULL,
                   ofsted_enrichment_requested_at = NULL
               WHERE id = %s AND ofsted_enrichment_requested_urn = %s""",
            (review_id, urn),
        )
        conn.commit()


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
                      customer_published_by, customer_published_at,
                      customer_lifecycle_stage, customer_lifecycle_reason,
                      customer_lifecycle_policy_version, customer_lifecycle_evaluated_at,
                      publication_automation_blocked, publication_automation_reason,
                      publication_automation_provenance
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
                      ai.recommendation, ai.confidence, ai.status, se.extracted_facts,
                      os.extracted_facts
               FROM opportunity_signals os JOIN raw_signals rs ON rs.id = os.raw_signal_id
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               LEFT JOIN LATERAL (SELECT recommendation, confidence, status FROM signal_ai_reviews
                 WHERE raw_signal_id = rs.id ORDER BY created_at DESC LIMIT 1) ai ON TRUE
               WHERE os.opportunity_id = %s ORDER BY rs.discovered_at""",
            (opportunity_id,),
        ).fetchall()
        family_rows = conn.execute(
            """SELECT r.raw_signal_id, f.id, f.planning_authority, f.raw_reference,
                      f.normalized_reference, f.origin_status, f.primary_signal_id,
                      r.relationship_type, attempt.status, attempt.attempted_at, attempt.details
               FROM planning_signal_family_relationships r
               JOIN planning_application_families f ON f.id = r.family_id
               LEFT JOIN LATERAL (
                 SELECT status, attempted_at, details FROM planning_origin_recovery_attempts
                 WHERE family_id = f.id ORDER BY attempted_at DESC, id DESC LIMIT 1
               ) attempt ON TRUE
               WHERE r.raw_signal_id = ANY(
                 SELECT raw_signal_id FROM opportunity_signals WHERE opportunity_id = %s
               )
               ORDER BY f.raw_reference, r.created_at""",
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
        lifecycle_history = conn.execute(
            """SELECT old_lifecycle, new_lifecycle, reason, triggering_signal_ids,
                      source_type, policy_version, actor_type, actor, created_at
               FROM opportunity_lifecycle_history WHERE opportunity_id = %s
               ORDER BY created_at DESC LIMIT 25""",
            (opportunity_id,),
        ).fetchall()
        planning_watches = conn.execute(
            """SELECT w.latest_outcome, w.last_checked_at, w.next_eligible_refresh_at,
                      w.last_provider_result, w.status_changed_at,
                      w.consecutive_provider_errors, w.enabled, f.planning_authority,
                      f.raw_reference
               FROM planning_lifecycle_watches w
               JOIN planning_application_families f ON f.id = w.family_id
               WHERE w.opportunity_id = %s ORDER BY w.next_eligible_refresh_at""",
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
        "extracted_facts",
        "relationship_extracted_facts",
    )
    signals = [dict(zip(fields, row)) for row in rows]
    family_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in family_rows:
        family_by_signal.setdefault(str(row[0]), []).append(
            {
                "id": str(row[1]),
                "planning_authority": row[2],
                "raw_reference": row[3],
                "normalized_reference": row[4],
                "origin_status": row[5],
                "primary_signal_id": str(row[6]) if row[6] else None,
                "relationship_type": row[7],
                "latest_recovery_status": row[8],
                "latest_recovery_at": row[9],
                "latest_recovery_details": row[10] or {},
            }
        )
    for signal in signals:
        signal["planning_families"] = family_by_signal.get(str(signal["id"]), [])
        signal.update(planning_timeline_projection(signal))
    active_signals = [signal for signal in signals if signal["relationship_status"] == "ACTIVE"]
    support_states = [classify_evidence_support(signal) for signal in active_signals]
    foundational_count = support_states.count(EvidenceSupport.FOUNDATIONAL)
    supporting_followup_count = support_states.count(EvidenceSupport.SUPPORTING_FOLLOWUP)
    other_lifecycle_count = support_states.count(EvidenceSupport.OTHER_LIFECYCLE)
    unresolved_origin_count = len(
        {
            family["id"]
            for signal in active_signals
            for family in signal.get("planning_families", [])
            if family["relationship_type"] == "REFERENCES_APPLICATION"
            and family["origin_status"] in {"MISSING", "AMBIGUOUS"}
        }
    )
    approved_customer_signals = [
        signal
        for signal in signals
        if signal.get("relationship_status") == "ACTIVE"
        and signal.get("review_status") == "APPROVED"
        and signal.get("source_type") != "procurement"
    ]
    authorities: list[str] = []
    regions: list[str] = []
    for signal in approved_customer_signals:
        metadata = signal.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        authority = (
            metadata.get("local_authority")
            or metadata.get("council")
            or (
                (metadata.get("authority") or {}).get("name")
                if isinstance(metadata.get("authority"), dict)
                else None
            )
        )
        if authority and str(authority) not in authorities:
            authorities.append(str(authority))
        region = metadata.get("region")
        if region and str(region) not in regions:
            regions.append(str(region))
    customer_projection = {
        "customer_title": opportunity[19],
        "customer_summary": opportunity[20],
        "town": opportunity[5],
        "postcode": opportunity[4],
        "local_authority": authorities[0] if len(authorities) == 1 else None,
        "region": regions[0] if len(regions) == 1 else None,
        "change_type": opportunity[7],
        "source_types": sorted(
            {str(signal["source_type"]) for signal in approved_customer_signals}
        ),
    }
    lifecycle_preview = derive_care_lifecycle(active_signals)
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
        "customer_lifecycle_stage": opportunity[23],
        "customer_lifecycle_reason": opportunity[24],
        "customer_lifecycle_policy_version": opportunity[25],
        "customer_lifecycle_evaluated_at": opportunity[26],
        "publication_automation_blocked": opportunity[27],
        "publication_automation_reason": opportunity[28],
        "publication_automation_provenance": opportunity[29] or {},
        "derived_customer_lifecycle": lifecycle_preview.lifecycle.value,
        "derived_customer_lifecycle_reason": lifecycle_preview.reason,
        "derived_customer_lifecycle_policy_version": CARE_LIFECYCLE_POLICY_VERSION,
        "default_customer_title": generated_customer_title(customer_projection),
        "default_customer_summary": generated_customer_summary(customer_projection),
        "signals": signals,
        "evidence_support": {
            "foundational": foundational_count,
            "supporting_followups": supporting_followup_count,
            "other_lifecycle": other_lifecycle_count,
            "unresolved_origins": unresolved_origin_count,
        },
        "organisation_evidence": [
            {
                "provider": row[0],
                "external_id": row[1],
                "status": row[2],
                "resolution_outcome": row[3],
                "resolution_confidence": row[4],
                "reason": row[5],
                "retrieved_at": row[6],
            }
            for row in organisation_evidence
        ],
        "lifecycle_history": [
            {
                "old_lifecycle": row[0],
                "new_lifecycle": row[1],
                "reason": row[2],
                "triggering_signal_ids": [str(value) for value in (row[3] or [])],
                "source_type": row[4],
                "policy_version": row[5],
                "actor_type": row[6],
                "actor": row[7],
                "created_at": row[8],
            }
            for row in lifecycle_history
        ],
        "planning_lifecycle_watches": [
            {
                "latest_outcome": row[0],
                "last_checked_at": row[1],
                "next_eligible_refresh_at": row[2],
                "last_provider_result": row[3],
                "status_changed_at": row[4],
                "consecutive_provider_errors": row[5],
                "enabled": row[6],
                "planning_authority": row[7],
                "planning_reference": row[8],
            }
            for row in planning_watches
        ],
    }


def set_opportunity_automation_block(
    settings: Settings,
    opportunity_id: str,
    *,
    blocked: bool,
    reason: str | None,
    actor: str,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()[:500] or None
    if blocked and not clean_reason:
        raise ValueError("automation block reason is required")
    with connection(settings) as conn:
        row = conn.execute(
            """UPDATE opportunities
               SET publication_automation_blocked = %s,
                   publication_automation_reason = %s,
                   updated_at = now()
               WHERE id = %s AND vertical = 'CHILDRENS_HOME'
               RETURNING id""",
            (blocked, clean_reason if blocked else None, opportunity_id),
        ).fetchone()
        if not row:
            raise ValueError("CareProspect opportunity not found")
        conn.execute(
            """INSERT INTO admin_audit_events
               (action, actor, target_type, details, vertical)
               VALUES ('care_publication_automation_block_changed', %s,
                       'opportunity', %s, 'CHILDRENS_HOME')""",
            (
                actor,
                Jsonb(
                    {
                        "opportunity_id": opportunity_id,
                        "blocked": blocked,
                        "reason": clean_reason if blocked else None,
                    }
                ),
            ),
        )
        if blocked:
            watches = conn.execute(
                """UPDATE planning_lifecycle_watches
                   SET enabled = FALSE, disabled_reason = 'manual_automation_block',
                       updated_at = now()
                   WHERE opportunity_id = %s AND enabled
                   RETURNING id, cadence_days""",
                (opportunity_id,),
            ).fetchall()
            for watch_id, cadence in watches:
                conn.execute(
                    """INSERT INTO planning_lifecycle_watch_history
                         (watch_id, action, old_enabled, new_enabled,
                          old_cadence_days, new_cadence_days, reason,
                          policy_version, actor)
                       VALUES (%s, 'DISABLED', TRUE, FALSE, %s, %s,
                               'manual_automation_block', %s, %s)""",
                    (
                        watch_id,
                        cadence,
                        cadence,
                        CARE_PLANNING_WATCHER_POLICY_VERSION,
                        actor,
                    ),
                )
        conn.commit()
    return {
        "id": opportunity_id,
        "publication_automation_blocked": blocked,
        "publication_automation_reason": clean_reason if blocked else None,
    }


def _cleanup_match_reviews(settings: Settings, *, limit: int, actor: str) -> dict[str, int]:
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
