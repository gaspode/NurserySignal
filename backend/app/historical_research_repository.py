from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from psycopg.types.json import Jsonb

from app.config import Settings
from app.db import connection
from app.historical_corpus import (
    CASE_LINK_CONFIDENCE,
    ELIGIBILITY_VALUES,
    NOT_FOUND_REASONS,
    evidence_eligibility,
    parsed_datetime,
    record_content_hash,
    replay_signal,
)
from app.repository import record_admin_audit
from app.verticals import CHILDRENS_HOME, validate_vertical

DEFAULT_CORPUS_VERSION = "care-historical-research-v2"
DEFAULT_MANIFEST = "care_historical_research_v2.json"
SUPPORTED_MANIFESTS = {
    "care_historical_research_v1.json": "care-ofsted-v1",
    "care_historical_research_v2.json": "care-ofsted-v2",
}
MAX_CASES = 50
MAX_RECORDS = 250


def _bundled_manifest(name: str = DEFAULT_MANIFEST) -> tuple[bytes, dict[str, Any]]:
    if name not in SUPPORTED_MANIFESTS:
        raise ValueError("unsupported historical corpus manifest")
    payload = files("app.data").joinpath(name).read_bytes()
    if len(payload) > 512 * 1024:
        raise ValueError("historical corpus manifest exceeds 512 KiB")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("historical corpus manifest must be an object")
    return payload, value


def _require_text(value: Any, name: str, maximum: int = 2000) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise ValueError(f"invalid {name}")
    return result


def import_bundled_historical_corpus(
    settings: Settings,
    *,
    actor: str,
    manifest_name: str = DEFAULT_MANIFEST,
) -> dict[str, Any]:
    """Import a fixed, code-reviewed evaluation corpus without touching live signals."""
    payload, manifest = _bundled_manifest(manifest_name)
    benchmark_version = _require_text(manifest.get("benchmark_version"), "benchmark_version", 80)
    corpus_version = _require_text(manifest.get("corpus_version"), "corpus_version", 80)
    vertical = validate_vertical(_require_text(manifest.get("vertical"), "vertical", 40))
    if vertical != CHILDRENS_HOME or benchmark_version != SUPPORTED_MANIFESTS[manifest_name]:
        raise ValueError("historical corpus manifest does not match its benchmark version")
    records = manifest.get("records") or []
    cases = manifest.get("cases") or []
    if not isinstance(records, list) or not isinstance(cases, list):
        raise ValueError("historical corpus records and cases must be arrays")
    if len(records) > MAX_RECORDS or len(cases) > MAX_CASES:
        raise ValueError("historical corpus exceeds bounded import limits")

    import hashlib

    manifest_sha = hashlib.sha256(payload).hexdigest()
    with connection(settings) as conn:
        existing = conn.execute(
            """SELECT id, summary FROM benchmark_research_runs
               WHERE benchmark_version = %s AND corpus_version = %s
                 AND manifest_sha256 = %s AND status = 'COMPLETE'""",
            (benchmark_version, corpus_version, manifest_sha),
        ).fetchone()
        if existing:
            return {
                "research_run_id": str(existing[0]),
                "benchmark_version": benchmark_version,
                "corpus_version": corpus_version,
                "summary": existing[1] or {},
                "idempotent": True,
            }
        run = conn.execute(
            """INSERT INTO benchmark_research_runs (
                   benchmark_version, corpus_version, vertical, status,
                   manifest_bucket, manifest_key, manifest_sha256, parameters, created_by
               ) VALUES (%s, %s, %s, 'IMPORTING', 'BUNDLED', %s, %s, %s, %s)
               RETURNING id""",
            (
                benchmark_version,
                corpus_version,
                vertical,
                manifest_name,
                manifest_sha,
                Jsonb({"max_cases": MAX_CASES, "max_records": MAX_RECORDS, "read_only": True}),
                actor,
            ),
        ).fetchone()
        run_id = str(run[0])
        benchmark_rows = conn.execute(
            """SELECT id, benchmark_case_id, outcome_date
               FROM benchmark_cases
               WHERE benchmark_version = %s AND vertical = %s""",
            (benchmark_version, vertical),
        ).fetchall()
        benchmark_cases = {
            row[1]: {"id": str(row[0]), "outcome_date": row[2]} for row in benchmark_rows
        }
        if len(benchmark_cases) != len(cases):
            raise ValueError("manifest does not cover the complete benchmark case set")

        record_ids: dict[tuple[str, str, str], str] = {}
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("historical record must be an object")
            item_vertical = validate_vertical(item.get("vertical") or vertical)
            if item_vertical != vertical:
                raise ValueError("historical record vertical differs from corpus")
            source_type = _require_text(item.get("source_type"), "source_type", 30).lower()
            if source_type not in {"planning", "recruitment"}:
                raise ValueError("historical corpus source must be planning or recruitment")
            provider = _require_text(item.get("provider"), "provider", 80)
            external_id = _require_text(item.get("external_id"), "external_id", 240)
            available_at = parsed_datetime(item.get("available_at"))
            retrieved_at = parsed_datetime(item.get("retrieved_at"))
            if available_at is None or retrieved_at is None:
                raise ValueError("historical record dates must be ISO timestamps")
            content_sha = record_content_hash({**item, "vertical": item_vertical})
            row = conn.execute(
                """INSERT INTO historical_research_records (
                       corpus_version, vertical, source_type, provider, external_id,
                       source_url, title, raw_text, organisation_hint, location_hint,
                       source_event_at, available_at, retrieved_at, metadata, provenance,
                       content_sha256
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (corpus_version, provider, source_type, external_id)
                   DO UPDATE SET content_sha256 = historical_research_records.content_sha256
                   RETURNING id, content_sha256""",
                (
                    corpus_version,
                    item_vertical,
                    source_type,
                    provider,
                    external_id,
                    _require_text(item.get("source_url"), "source_url", 2000),
                    _require_text(item.get("title"), "title", 500),
                    _require_text(item.get("raw_text"), "raw_text", 20_000),
                    item.get("organisation_hint"),
                    item.get("location_hint"),
                    parsed_datetime(item.get("source_event_at")),
                    available_at,
                    retrieved_at,
                    Jsonb(item.get("metadata") or {}),
                    Jsonb(item.get("provenance") or {}),
                    content_sha,
                ),
            ).fetchone()
            if row[1] != content_sha:
                raise ValueError("historical source identity changed within one corpus version")
            record_ids[(provider, source_type, external_id)] = str(row[0])

        eligibility_counts: dict[str, int] = {}
        planning_cases: set[str] = set()
        recruitment_cases: set[str] = set()
        accepted_planning = 0
        accepted_recruitment = 0
        rejected = 0
        for case in cases:
            case_key = _require_text(case.get("benchmark_case_id"), "benchmark_case_id", 120)
            benchmark_case = benchmark_cases.pop(case_key, None)
            if benchmark_case is None:
                raise ValueError("manifest references an unknown or duplicate benchmark case")
            candidates = case.get("candidates") or []
            if not isinstance(candidates, list):
                raise ValueError("case candidates must be an array")
            accepted = 0
            rejected_for_case = 0
            research = conn.execute(
                """INSERT INTO benchmark_case_research (
                       research_run_id, benchmark_case_id, status, sources_searched,
                       search_strategy, records_found, records_accepted, records_rejected,
                       not_found_reason, notes, researched_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, 0, 0, %s, %s, %s)
                   RETURNING id""",
                (
                    run_id,
                    benchmark_case["id"],
                    _require_text(case.get("status") or "COMPLETE", "research status", 20),
                    Jsonb(case.get("sources_searched") or []),
                    Jsonb(case.get("search_strategy") or {}),
                    len(candidates),
                    case.get("not_found_reason"),
                    case.get("notes"),
                    parsed_datetime(case.get("researched_at") or manifest.get("researched_at")),
                ),
            ).fetchone()
            for candidate in candidates:
                confidence = _require_text(
                    candidate.get("case_link_confidence"), "case_link_confidence", 40
                )
                if confidence not in CASE_LINK_CONFIDENCE:
                    raise ValueError("invalid case-link confidence")
                identity = (
                    _require_text(candidate.get("provider"), "candidate provider", 80),
                    _require_text(candidate.get("source_type"), "candidate source_type", 30),
                    _require_text(candidate.get("external_id"), "candidate external_id", 240),
                )
                record_id = record_ids.get(identity)
                if not record_id:
                    raise ValueError("candidate references an unknown historical record")
                record = next(
                    item
                    for item in records
                    if (item["provider"], item["source_type"], item["external_id"]) == identity
                )
                calculated = evidence_eligibility(
                    outcome_at=benchmark_case["outcome_date"],
                    available_at=record.get("available_at"),
                    source_url=str(record.get("source_url") or ""),
                    provider=identity[0],
                    case_link_confidence=confidence,
                )
                declared = candidate.get("eligibility") or calculated
                if declared not in ELIGIBILITY_VALUES or declared != calculated:
                    raise ValueError("declared historical eligibility is not reproducible")
                conn.execute(
                    """INSERT INTO benchmark_case_research_evidence (
                           research_run_id, benchmark_case_id, historical_record_id,
                           case_link_confidence, eligibility, link_reason, rejection_reason
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run_id,
                        benchmark_case["id"],
                        record_id,
                        confidence,
                        calculated,
                        _require_text(candidate.get("link_reason"), "link_reason", 1000),
                        candidate.get("rejection_reason"),
                    ),
                )
                eligibility_counts[calculated] = eligibility_counts.get(calculated, 0) + 1
                if calculated == "ELIGIBLE":
                    accepted += 1
                    if identity[1] == "planning":
                        accepted_planning += 1
                        planning_cases.add(case_key)
                    else:
                        accepted_recruitment += 1
                        recruitment_cases.add(case_key)
                else:
                    rejected += 1
                    rejected_for_case += 1
            not_found = case.get("not_found_reason")
            if not_found is not None and not_found not in NOT_FOUND_REASONS:
                raise ValueError("invalid not-found reason")
            conn.execute(
                """UPDATE benchmark_case_research
                   SET records_accepted = %s, records_rejected = %s WHERE id = %s""",
                (accepted, rejected_for_case, research[0]),
            )

        summary = {
            "benchmark_cases": len(cases),
            "cases_with_planning": len(planning_cases),
            "cases_with_recruitment": len(recruitment_cases),
            "cases_with_both": len(planning_cases & recruitment_cases),
            "cases_with_neither": len(cases) - len(planning_cases | recruitment_cases),
            "accepted_planning_items": accepted_planning,
            "accepted_recruitment_items": accepted_recruitment,
            "candidate_items_rejected": rejected,
            "eligibility_counts": eligibility_counts,
        }
        conn.execute(
            """UPDATE benchmark_research_runs
               SET status = 'COMPLETE', summary = %s, completed_at = now() WHERE id = %s""",
            (Jsonb(summary), run_id),
        )
        conn.commit()
    audit_id = record_admin_audit(
        settings,
        action="historical_corpus_import",
        actor=actor,
        target_type="benchmark_research_run",
        details={
            "research_run_id": run_id,
            "benchmark_version": benchmark_version,
            "corpus_version": corpus_version,
            "summary": summary,
            "bounded": True,
            "production_state_mutated": False,
        },
    )
    return {
        "research_run_id": run_id,
        "benchmark_version": benchmark_version,
        "corpus_version": corpus_version,
        "summary": summary,
        "operation_id": audit_id,
        "idempotent": False,
    }


def latest_historical_corpus(
    settings: Settings, *, benchmark_version: str, vertical: str
) -> dict[str, Any] | None:
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT id, corpus_version, summary, completed_at
               FROM benchmark_research_runs
               WHERE benchmark_version = %s AND vertical = %s AND status = 'COMPLETE'
               ORDER BY completed_at DESC, id DESC LIMIT 1""",
            (benchmark_version, validate_vertical(vertical)),
        ).fetchone()
    if not row:
        return None
    return {
        "research_run_id": str(row[0]),
        "corpus_version": row[1],
        "summary": row[2] or {},
        "completed_at": row[3],
    }


def historical_corpus_inputs(
    settings: Settings,
    *,
    research_run_id: str,
    cases: list[dict[str, Any]],
    max_signals: int,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    case_ids = [str(case["id"]) for case in cases]
    if not case_ids:
        return [], {}
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT DISTINCT hrr.id, hrr.corpus_version, hrr.vertical,
                      hrr.source_type, hrr.provider, hrr.external_id, hrr.source_url,
                      hrr.title, hrr.raw_text, hrr.organisation_hint, hrr.location_hint,
                      hrr.source_event_at, hrr.available_at, hrr.retrieved_at,
                      hrr.metadata, hrr.provenance, bcre.benchmark_case_id
               FROM benchmark_case_research_evidence bcre
               JOIN historical_research_records hrr ON hrr.id = bcre.historical_record_id
               WHERE bcre.research_run_id = %s AND bcre.eligibility = 'ELIGIBLE'
                 AND bcre.benchmark_case_id = ANY(%s::uuid[])
               ORDER BY hrr.available_at, hrr.id LIMIT %s""",
            (research_run_id, case_ids, min(max(int(max_signals), 1), 1000)),
        ).fetchall()
    fields = (
        "id",
        "corpus_version",
        "vertical",
        "source_type",
        "provider",
        "external_id",
        "source_url",
        "title",
        "raw_text",
        "organisation_hint",
        "location_hint",
        "source_event_at",
        "available_at",
        "retrieved_at",
        "metadata",
        "provenance",
        "benchmark_case_id",
    )
    signals: dict[str, dict[str, Any]] = {}
    links: dict[str, list[str]] = {}
    for row in rows:
        item = dict(zip(fields, row))
        signal = replay_signal(item)
        signals[str(item["id"])] = signal
        links.setdefault(str(item["benchmark_case_id"]), []).append(str(item["id"]))
    return list(signals.values()), links


def historical_research_summary(
    settings: Settings, *, benchmark_version: str, vertical: str
) -> dict[str, Any] | None:
    corpus = latest_historical_corpus(
        settings, benchmark_version=benchmark_version, vertical=vertical
    )
    if not corpus:
        return None
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT bc.benchmark_case_id, bc.known_operator, bc.outcome_date,
                      bcr.status, bcr.records_found, bcr.records_accepted,
                      bcr.records_rejected, bcr.not_found_reason, bcr.notes,
                      count(*) FILTER (WHERE bcre.eligibility = 'ELIGIBLE'
                                      AND hrr.source_type = 'planning'),
                      count(*) FILTER (WHERE bcre.eligibility = 'ELIGIBLE'
                                      AND hrr.source_type = 'recruitment'),
                      jsonb_agg(jsonb_build_object(
                          'source_type', hrr.source_type,
                          'title', hrr.title,
                          'available_at', hrr.available_at,
                          'source_url', hrr.source_url,
                          'case_link_confidence', bcre.case_link_confidence,
                          'eligibility', bcre.eligibility,
                          'link_reason', bcre.link_reason,
                          'rejection_reason', bcre.rejection_reason
                      ) ORDER BY hrr.available_at) FILTER (WHERE hrr.id IS NOT NULL)
               FROM benchmark_case_research bcr
               JOIN benchmark_cases bc ON bc.id = bcr.benchmark_case_id
               LEFT JOIN benchmark_case_research_evidence bcre
                      ON bcre.research_run_id = bcr.research_run_id
                     AND bcre.benchmark_case_id = bcr.benchmark_case_id
               LEFT JOIN historical_research_records hrr ON hrr.id = bcre.historical_record_id
               WHERE bcr.research_run_id = %s
               GROUP BY bc.benchmark_case_id, bc.known_operator, bc.outcome_date,
                        bcr.status, bcr.records_found, bcr.records_accepted,
                        bcr.records_rejected, bcr.not_found_reason, bcr.notes
               ORDER BY bc.outcome_date, bc.benchmark_case_id""",
            (corpus["research_run_id"],),
        ).fetchall()
    fields = (
        "benchmark_case_id",
        "known_operator",
        "outcome_date",
        "status",
        "records_found",
        "records_accepted",
        "records_rejected",
        "not_found_reason",
        "notes",
        "eligible_planning",
        "eligible_recruitment",
        "evidence",
    )
    return {**corpus, "cases": [dict(zip(fields, row)) for row in rows]}
