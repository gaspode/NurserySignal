from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.backtesting import (
    BACKTEST_ENGINE_VERSION,
    BacktestBounds,
    aggregate_results,
    compare_metrics,
    replay_case,
    run_fingerprint,
)
from app.config import Settings
from app.db import connection
from app.repository import record_admin_audit
from app.verticals import ALL_VERTICALS, CHILDRENS_HOME, validate_vertical, validate_vertical_filter

CARE_BENCHMARK_VERSION = "care-ofsted-v1"


def seed_care_ofsted_benchmark(
    settings: Settings,
    *,
    actor: str,
    benchmark_version: str = CARE_BENCHMARK_VERSION,
    limit: int = 30,
) -> dict[str, Any]:
    """Seed bounded authoritative registration outcomes from preserved Ofsted rows.

    This writes benchmark truth only. It does not change signals, opportunities,
    reviews or lifecycle state, and no Ofsted value becomes a replay input.
    """
    limit = min(max(int(limit), 1), 30)
    if not benchmark_version or len(benchmark_version) > 80:
        raise ValueError("invalid benchmark_version")
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT rs.id, rs.external_id, rs.organisation_hint,
                      rs.metadata->>'ofsted_urn', rs.metadata->>'registration_date',
                      rs.metadata->>'local_authority', rs.source_url
               FROM raw_signals rs
               WHERE rs.vertical = 'CHILDRENS_HOME'
                 AND rs.source_type = 'ofsted'
                 AND rs.metadata->>'ofsted_urn' IS NOT NULL
                 AND rs.metadata->>'registration_date' ~ '^20(25|26)-[0-9]{2}-[0-9]{2}$'
                 AND COALESCE(rs.organisation_hint, '') <> ''
               ORDER BY (rs.metadata->>'registration_date')::date DESC, rs.id
               LIMIT %s""",
            (limit,),
        ).fetchall()
        inserted = 0
        existing = 0
        for signal_id, external_id, operator, urn, registration_date, authority, source_url in rows:
            row = conn.execute(
                """INSERT INTO benchmark_cases (
                       benchmark_case_id, benchmark_version, vertical, known_operator,
                       known_location, known_regulatory_id, outcome_type, outcome_date,
                       provenance, label_confidence, notes
                   ) VALUES (%s, %s, 'CHILDRENS_HOME', %s, %s, %s, 'REGISTERED',
                             %s::date, %s, 'HIGH', %s)
                   ON CONFLICT (benchmark_version, benchmark_case_id) DO NOTHING
                   RETURNING id""",
                (
                    f"ofsted:{urn}",
                    benchmark_version,
                    operator,
                    authority,
                    urn,
                    registration_date,
                    Jsonb(
                        {
                            "source": "OFSTED_REGISTER",
                            "raw_signal_id": str(signal_id),
                            "external_id": external_id,
                            "source_url": source_url,
                            "truth_role": "OUTCOME_ONLY",
                        }
                    ),
                    (
                        "Official Ofsted registration date; Ofsted is excluded from "
                        "pre-registration replay."
                    ),
                ),
            ).fetchone()
            if row:
                inserted += 1
            else:
                existing += 1
        conn.commit()
    audit_id = record_admin_audit(
        settings,
        action="backtest_benchmark_seed",
        actor=actor,
        target_type="benchmark_case",
        details={
            "benchmark_version": benchmark_version,
            "vertical": CHILDRENS_HOME,
            "selected": len(rows),
            "inserted": inserted,
            "existing": existing,
            "bounded": True,
        },
    )
    return {
        "benchmark_version": benchmark_version,
        "selected": len(rows),
        "inserted": inserted,
        "existing": existing,
        "operation_id": audit_id,
    }


def _case_rows(
    settings: Settings, *, benchmark_version: str, vertical: str, limit: int
) -> list[dict[str, Any]]:
    vertical = validate_vertical(vertical)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, benchmark_case_id, benchmark_version, vertical,
                      known_operator, known_site, known_location, known_postcode,
                      known_company_number, known_regulatory_id, outcome_type,
                      outcome_date, provenance, label_confidence, notes
               FROM benchmark_cases
               WHERE benchmark_version = %s AND vertical = %s
               ORDER BY outcome_date, benchmark_case_id
               LIMIT %s""",
            (benchmark_version, vertical, limit),
        ).fetchall()
    fields = (
        "id",
        "benchmark_case_id",
        "benchmark_version",
        "vertical",
        "known_operator",
        "known_site",
        "known_location",
        "known_postcode",
        "known_company_number",
        "known_regulatory_id",
        "outcome_type",
        "outcome_date",
        "provenance",
        "label_confidence",
        "notes",
    )
    return [dict(zip(fields, row)) for row in rows]


def _input_rows(
    settings: Settings, *, vertical: str, cases: list[dict[str, Any]], bounds: BacktestBounds
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not cases:
        return [], []
    earliest = min(case["outcome_date"] for case in cases)
    with connection(settings) as conn:
        rows = conn.execute(
            """SELECT id, schema_version, source_type, source_url, external_id,
                      discovered_at, title, raw_text, location_hint, organisation_hint,
                      metadata, vertical, persisted_at, created_at
               FROM raw_signals
               WHERE vertical = %s
                 AND source_type IN ('planning', 'recruitment', 'ofsted')
                 AND discovered_at >= %s::date - (%s * interval '1 day')
                 AND discovered_at <= %s::timestamptz
               ORDER BY discovered_at, id
               LIMIT %s""",
            (vertical, earliest, bounds.lookback_days, bounds.as_of, bounds.max_signals),
        ).fetchall()
        evidence_rows = conn.execute(
            """SELECT oe.operator_id, oe.retrieved_at, oe.safe_metadata
               FROM organisation_evidence oe
               WHERE oe.provider = 'COMPANIES_HOUSE'
                 AND oe.retrieved_at <= %s::timestamptz
               ORDER BY oe.retrieved_at, oe.id
               LIMIT %s""",
            (bounds.as_of, bounds.max_signals),
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
        "persisted_at",
        "created_at",
    )
    return (
        [dict(zip(fields, row)) for row in rows],
        [
            {"operator_id": row[0], "retrieved_at": row[1], "safe_metadata": row[2] or {}}
            for row in evidence_rows
        ],
    )


def execute_backtest(
    settings: Settings,
    *,
    actor: str,
    benchmark_version: str,
    vertical: str,
    bounds: BacktestBounds,
) -> dict[str, Any]:
    vertical = validate_vertical(vertical)
    if not benchmark_version or len(benchmark_version) > 80:
        raise ValueError("invalid benchmark_version")
    fingerprint = run_fingerprint(
        benchmark_version=benchmark_version, vertical=vertical, bounds=bounds
    )
    with connection(settings) as conn:
        existing = conn.execute(
            "SELECT id FROM backtest_runs WHERE run_fingerprint = %s", (fingerprint,)
        ).fetchone()
    if existing:
        detail = get_backtest_run(settings, str(existing[0]))
        if detail is None:
            raise RuntimeError("backtest run disappeared")
        return {**detail, "idempotent": True}

    cases = _case_rows(
        settings,
        benchmark_version=benchmark_version,
        vertical=vertical,
        limit=bounds.max_cases,
    )
    if not cases:
        raise ValueError("benchmark version has no cases for this vertical")
    signals, company_evidence = _input_rows(settings, vertical=vertical, cases=cases, bounds=bounds)
    params = {
        "lookback_days": bounds.lookback_days,
        "max_cases": bounds.max_cases,
        "max_signals": bounds.max_signals,
        "outcome_source_mode": "OFSTED_OUTCOME_ONLY"
        if vertical == CHILDRENS_HOME
        else "SOURCE_SPECIFIC",
    }
    with connection(settings) as conn:
        row = conn.execute(
            """INSERT INTO backtest_runs (
                   run_fingerprint, benchmark_version, engine_version, vertical,
                   as_of, status, parameters
               ) VALUES (%s, %s, %s, %s, %s, 'RUNNING', %s)
               ON CONFLICT (run_fingerprint) DO NOTHING RETURNING id""",
            (
                fingerprint,
                benchmark_version,
                BACKTEST_ENGINE_VERSION,
                vertical,
                bounds.as_of,
                Jsonb(params),
            ),
        ).fetchone()
        conn.commit()
    if row is None:
        with connection(settings) as conn:
            row = conn.execute(
                "SELECT id FROM backtest_runs WHERE run_fingerprint = %s", (fingerprint,)
            ).fetchone()
        return {**(get_backtest_run(settings, str(row[0])) or {}), "idempotent": True}
    run_id = str(row[0])
    try:
        results = [replay_case(case, signals, company_evidence, bounds) for case in cases]
        metrics, contribution = aggregate_results(results)
        with connection(settings) as conn:
            for result in results:
                conn.execute(
                    """INSERT INTO backtest_case_results (
                           run_id, benchmark_case_id, usable, exclusion_reason, detected,
                           opportunity_created, first_discovered_at, first_opportunity_at,
                           first_source, lead_time_days, organisation_resolution,
                           organisation_resolved_at, companies_house_improved_identity,
                           site_resolution, duplicate_opportunities, incorrect_merges,
                           review_items, source_dates, timeline, generated_opportunities,
                           confidence_metrics
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                           %s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s
                       )""",
                    (
                        run_id,
                        result["benchmark_case_id"],
                        result["usable"],
                        result.get("exclusion_reason"),
                        result["detected"],
                        result["opportunity_created"],
                        result.get("first_discovered_at"),
                        result.get("first_opportunity_at"),
                        result.get("first_source"),
                        result.get("lead_time_days"),
                        result.get("organisation_resolution"),
                        result.get("organisation_resolved_at"),
                        result.get("companies_house_improved_identity", False),
                        result.get("site_resolution"),
                        result.get("duplicate_opportunities", 0),
                        result.get("incorrect_merges", 0),
                        result.get("review_items", 0),
                        Jsonb(result.get("source_dates") or {}),
                        Jsonb(result.get("timeline") or []),
                        Jsonb(result.get("generated_opportunities") or []),
                        Jsonb(result.get("confidence_metrics") or {}),
                    ),
                )
            conn.execute(
                """UPDATE backtest_runs
                   SET status = 'SUCCESS', metrics = %s, source_contribution = %s,
                       completed_at = now()
                   WHERE id = %s""",
                (Jsonb(metrics), Jsonb(contribution), run_id),
            )
            conn.commit()
    except Exception as exc:
        with connection(settings) as conn:
            conn.execute(
                """UPDATE backtest_runs SET status = 'FAILED', failure_category = %s,
                          failure_message = %s, completed_at = now() WHERE id = %s""",
                (type(exc).__name__, str(exc)[:240].replace("\n", " "), run_id),
            )
            conn.commit()
        raise
    record_admin_audit(
        settings,
        action="historical_backtest_run",
        actor=actor,
        target_type="benchmark_run",
        details={
            "run_id": run_id,
            "benchmark_version": benchmark_version,
            "engine_version": BACKTEST_ENGINE_VERSION,
            "vertical": vertical,
            "parameters": params,
            "metrics": metrics,
            "bounded": True,
            "production_state_mutated": False,
        },
    )
    detail = get_backtest_run(settings, run_id)
    return {**(detail or {}), "idempotent": False}


def get_backtest_run(settings: Settings, run_id: str) -> dict[str, Any] | None:
    with connection(settings) as conn:
        row = conn.execute(
            """SELECT id, benchmark_version, engine_version, vertical, as_of, status,
                      parameters, metrics, source_contribution, failure_category,
                      failure_message, started_at, completed_at
               FROM backtest_runs WHERE id = %s""",
            (run_id,),
        ).fetchone()
        if not row:
            return None
        results = conn.execute(
            """SELECT bcr.benchmark_case_id, bc.benchmark_case_id, bc.known_operator,
                      bc.known_location, bc.known_regulatory_id, bc.outcome_type,
                      bc.outcome_date, bc.label_confidence, bcr.usable,
                      bcr.exclusion_reason, bcr.detected, bcr.opportunity_created,
                      bcr.first_discovered_at, bcr.first_opportunity_at, bcr.first_source,
                      bcr.lead_time_days, bcr.organisation_resolution,
                      bcr.organisation_resolved_at, bcr.companies_house_improved_identity,
                      bcr.site_resolution, bcr.duplicate_opportunities,
                      bcr.incorrect_merges, bcr.review_items, bcr.source_dates,
                      bcr.timeline, bcr.confidence_metrics
               FROM backtest_case_results bcr
               JOIN benchmark_cases bc ON bc.id = bcr.benchmark_case_id
               WHERE bcr.run_id = %s
               ORDER BY bc.outcome_date, bc.benchmark_case_id""",
            (run_id,),
        ).fetchall()
    fields = (
        "benchmark_case_uuid",
        "benchmark_case_id",
        "known_operator",
        "known_location",
        "known_regulatory_id",
        "outcome_type",
        "outcome_date",
        "label_confidence",
        "usable",
        "exclusion_reason",
        "detected",
        "opportunity_created",
        "first_discovered_at",
        "first_opportunity_at",
        "first_source",
        "lead_time_days",
        "organisation_resolution",
        "organisation_resolved_at",
        "companies_house_improved_identity",
        "site_resolution",
        "duplicate_opportunities",
        "incorrect_merges",
        "review_items",
        "source_dates",
        "timeline",
        "confidence_metrics",
    )
    run_fields = (
        "id",
        "benchmark_version",
        "engine_version",
        "vertical",
        "as_of",
        "status",
        "parameters",
        "metrics",
        "source_contribution",
        "failure_category",
        "failure_message",
        "started_at",
        "completed_at",
    )
    return {
        **dict(zip(run_fields, row)),
        "case_results": [dict(zip(fields, item)) for item in results],
    }


def backtesting_summary(
    settings: Settings, *, vertical: str, benchmark_version: str | None = None
) -> dict[str, Any]:
    vertical_filter = validate_vertical_filter(vertical)
    clauses = ["TRUE"]
    params: list[Any] = []
    if vertical_filter != ALL_VERTICALS:
        clauses.append("vertical = %s")
        params.append(vertical_filter)
    if benchmark_version:
        clauses.append("benchmark_version = %s")
        params.append(benchmark_version)
    where = " AND ".join(clauses)
    with connection(settings) as conn:
        case_rows = conn.execute(
            f"""SELECT benchmark_version, vertical, count(*), min(outcome_date), max(outcome_date)
                FROM benchmark_cases WHERE {where}
                GROUP BY benchmark_version, vertical ORDER BY benchmark_version, vertical""",
            params,
        ).fetchall()
        run_rows = conn.execute(
            f"""SELECT id, benchmark_version, engine_version, vertical, as_of, status,
                       metrics, source_contribution, started_at, completed_at
                FROM backtest_runs WHERE {where}
                ORDER BY started_at DESC LIMIT 20""",
            params,
        ).fetchall()
    return {
        "benchmarks": [
            {
                "benchmark_version": row[0],
                "vertical": row[1],
                "case_count": row[2],
                "from_date": row[3],
                "to_date": row[4],
            }
            for row in case_rows
        ],
        "runs": [
            dict(
                zip(
                    (
                        "id",
                        "benchmark_version",
                        "engine_version",
                        "vertical",
                        "as_of",
                        "status",
                        "metrics",
                        "source_contribution",
                        "started_at",
                        "completed_at",
                    ),
                    row,
                )
            )
            for row in run_rows
        ],
        "default_benchmark_version": CARE_BENCHMARK_VERSION,
        "engine_version": BACKTEST_ENGINE_VERSION,
    }


def compare_backtest_runs(settings: Settings, left_id: str, right_id: str) -> dict[str, Any]:
    left = get_backtest_run(settings, left_id)
    right = get_backtest_run(settings, right_id)
    if left is None or right is None:
        raise ValueError("backtest run not found")
    return {
        "left": left,
        "right": right,
        "delta": compare_metrics(left["metrics"], right["metrics"]),
    }


def labelled_decisions(settings: Settings, *, vertical: str, limit: int = 100) -> dict[str, Any]:
    vertical_filter = validate_vertical_filter(vertical)
    limit = min(max(int(limit), 1), 500)
    clauses = ["TRUE"]
    params: list[Any] = []
    if vertical_filter != ALL_VERTICALS:
        clauses.append("vertical = %s")
        params.append(vertical_filter)
    with connection(settings) as conn:
        rows = conn.execute(
            f"""SELECT decision_id, vertical, decision_type, left_record_id,
                       right_record_id, decision, decided_at, actor, features
                FROM backtest_labelled_decisions WHERE {" AND ".join(clauses)}
                ORDER BY decided_at DESC LIMIT %s""",
            [*params, limit],
        ).fetchall()
    fields = (
        "decision_id",
        "vertical",
        "decision_type",
        "left_record_id",
        "right_record_id",
        "decision",
        "decided_at",
        "actor",
        "features",
    )
    return {"items": [dict(zip(fields, row)) for row in rows], "limit": limit}
