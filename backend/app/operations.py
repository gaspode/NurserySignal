from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.care_lifecycle import (
    CARE_PLANNING_WATCHER_POLICY_VERSION,
    CARE_PUBLICATION_POLICY_VERSION,
    CARE_WITHDRAWAL_POLICY_VERSION,
    evaluate_publication,
    project_planning_watch_requests,
)
from app.config import Settings
from app.db import connection
from app.repository import (
    _care_withdrawal_preview_from_inventory,
    _current_care_publication_inventory,
)

OPERATIONS_SUMMARY_SCHEMA_VERSION = "signalhub-operations-summary-v1"
WINDOWS = ("24h", "7d", "30d")


def _counts(rows: list[tuple[Any, ...]], key_index: int, value_index: int) -> dict[str, int]:
    return {str(row[key_index] or "UNKNOWN"): int(row[value_index] or 0) for row in rows}


def _rate(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate_percent": round(numerator / denominator * 100, 1) if denominator else None,
    }


def _window_payload(rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for label in WINDOWS:
        relevant = [row for row in rows if row[0] == label]
        by_vertical: dict[str, dict[str, int]] = {}
        by_source: dict[str, dict[str, int]] = {}
        totals = Counter()
        for _, vertical, source_type, collected, accepted, rejected, pending in relevant:
            values = {
                "records_collected": int(collected or 0),
                "signals_accepted": int(accepted or 0),
                "signals_rejected": int(rejected or 0),
                "pending_review": int(pending or 0),
            }
            by_vertical[str(vertical)] = dict(
                Counter(by_vertical.get(str(vertical), {})) + Counter(values)
            )
            by_source[str(source_type)] = dict(
                Counter(by_source.get(str(source_type), {})) + Counter(values)
            )
            totals.update(values)
        payload[label] = {
            **dict(totals),
            "ingestion_failures": None,
            "ingestion_failures_available": False,
            "by_vertical": dict(sorted(by_vertical.items())),
            "by_source_type": dict(sorted(by_source.items())),
        }
    return payload


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def operations_summary(settings: Settings) -> dict[str, Any]:
    """Return one bounded, read-only operational snapshot from persisted data."""
    inventory = _current_care_publication_inventory(settings)
    withdrawal_preview = _care_withdrawal_preview_from_inventory(inventory)
    decisions = inventory["decisions"]
    hygiene_counts = Counter(item["hygiene"]["category"] for item in decisions)
    publication_outcomes = Counter(item["decision"].outcome for item in decisions)

    with connection(settings) as conn:
        ingestion_rows = conn.execute(
            """WITH windows(label, since_at) AS (
                 VALUES ('24h', now() - interval '24 hours'),
                        ('7d', now() - interval '7 days'),
                        ('30d', now() - interval '30 days')
               )
               SELECT w.label, rs.vertical, rs.source_type,
                      count(*),
                      count(*) FILTER (WHERE se.review_status = 'APPROVED'),
                      count(*) FILTER (WHERE se.review_status = 'REJECTED'),
                      count(*) FILTER (WHERE se.review_status = 'PENDING')
               FROM windows w
               JOIN raw_signals rs ON rs.discovered_at >= w.since_at
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               GROUP BY w.label, rs.vertical, rs.source_type
               ORDER BY w.label, rs.vertical, rs.source_type"""
        ).fetchall()
        signal_rows = conn.execute(
            """SELECT rs.vertical, COALESCE(se.review_status, 'UNENRICHED'), count(*)
               FROM raw_signals rs
               LEFT JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               GROUP BY rs.vertical, COALESCE(se.review_status, 'UNENRICHED')"""
        ).fetchall()
        unmatched_rows = conn.execute(
            """SELECT rs.vertical, count(*)
               FROM raw_signals rs
               JOIN signal_enrichments se ON se.raw_signal_id = rs.id
               WHERE se.review_status = 'APPROVED'
                 AND COALESCE(se.extracted_facts->>'likely_false_positive', 'false') <> 'true'
                 AND COALESCE(se.extracted_facts->>'opportunity_creation_decision', '')
                     <> 'IGNORE_FOR_OPPORTUNITY'
                 AND NOT EXISTS (
                   SELECT 1 FROM opportunity_signals os
                   WHERE os.raw_signal_id = rs.id AND os.status = 'ACTIVE'
                 )
               GROUP BY rs.vertical"""
        ).fetchall()
        opportunity_rows = conn.execute(
            """SELECT vertical, count(*)
               FROM opportunities
               WHERE review_status NOT IN ('MERGED', 'REJECTED')
                 AND COALESCE(customer_lifecycle_stage, '') <> 'STOPPED'
               GROUP BY vertical"""
        ).fetchall()
        match_rows = conn.execute(
            """SELECT vertical, count(*) FROM opportunity_match_reviews
               WHERE status = 'PENDING' GROUP BY vertical"""
        ).fetchall()
        lifecycle_rows = conn.execute(
            """SELECT COALESCE(customer_lifecycle_stage, 'UNSET'), count(*)
               FROM opportunities WHERE vertical = 'CHILDRENS_HOME'
               GROUP BY COALESCE(customer_lifecycle_stage, 'UNSET')"""
        ).fetchall()
        transition_rows = conn.execute(
            """WITH windows(label, since_at) AS (
                 VALUES ('24h', now() - interval '24 hours'),
                        ('7d', now() - interval '7 days'),
                        ('30d', now() - interval '30 days')
               )
               SELECT w.label, COALESCE(h.old_lifecycle, 'UNSET'), h.new_lifecycle, count(*)
               FROM windows w
               JOIN opportunity_lifecycle_history h ON h.created_at >= w.since_at
               JOIN opportunities o ON o.id = h.opportunity_id
               WHERE o.vertical = 'CHILDRENS_HOME'
               GROUP BY w.label, COALESCE(h.old_lifecycle, 'UNSET'), h.new_lifecycle"""
        ).fetchall()
        recent_activity_rows = conn.execute(
            """WITH windows(label, since_at) AS (
                 VALUES ('24h', now() - interval '24 hours'),
                        ('7d', now() - interval '7 days'),
                        ('30d', now() - interval '30 days')
               )
               SELECT w.label,
                      (SELECT count(*) FROM opportunities o WHERE o.created_at >= w.since_at),
                      (SELECT count(*) FROM opportunity_signal_history h
                       WHERE h.created_at >= w.since_at AND h.action ILIKE '%LINK%'),
                      (SELECT count(*) FROM admin_audit_events a
                       WHERE a.created_at >= w.since_at AND a.action ILIKE '%merge%')
               FROM windows w"""
        ).fetchall()
        watcher_row = conn.execute(
            """SELECT s.execution_enabled, s.policy_version, s.emergency_reason,
                      s.max_polls_per_execution, s.max_provider_requests_per_day,
                      s.max_provider_requests_per_month,
                      count(w.id), count(w.id) FILTER (WHERE w.enabled),
                      count(w.id) FILTER (WHERE w.enabled AND w.next_eligible_refresh_at <= now()),
                      min(w.next_eligible_refresh_at) FILTER (WHERE w.enabled),
                      count(w.id) FILTER (WHERE w.disabled_reason IN
                        ('terminal_planning_outcome', 'lifecycle_terminal'))
               FROM planning_lifecycle_watcher_state s
               LEFT JOIN planning_lifecycle_watches w ON TRUE
               WHERE s.singleton
               GROUP BY s.execution_enabled, s.policy_version, s.emergency_reason,
                        s.max_polls_per_execution, s.max_provider_requests_per_day,
                        s.max_provider_requests_per_month"""
        ).fetchone()
        cadence_rows = conn.execute(
            """SELECT cadence_days, count(*) FROM planning_lifecycle_watches
               WHERE enabled GROUP BY cadence_days ORDER BY cadence_days"""
        ).fetchall()
        watcher_run_row = conn.execute(
            """SELECT
                 COALESCE(sum(provider_requests) FILTER (
                   WHERE created_at >=
                     date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                 ), 0),
                 COALESCE(sum(provider_requests) FILTER (
                   WHERE created_at >=
                     date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                 ), 0),
                 count(*) FILTER (WHERE status IN ('CHANGED', 'UNCHANGED')),
                 count(*) FILTER (WHERE status = 'UNCHANGED'),
                 count(*) FILTER (WHERE status = 'CHANGED'),
                 count(*) FILTER (WHERE status IN ('FAILED', 'RATE_LIMITED', 'QUEUE_FAILED'))
               FROM planning_lifecycle_watch_runs"""
        ).fetchone()
        recent_watcher_runs = conn.execute(
            """SELECT id, status, provider_requests, error_category, created_at,
                      completed_at, details->>'policy_version'
               FROM planning_lifecycle_watch_runs ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()
        publication_state = conn.execute(
            """SELECT execution_enabled, recurring_enabled, policy_version,
                      emergency_reason, max_publications_per_execution,
                      last_execution_at, last_selected, last_published,
                      last_skipped, last_failed,
                      (SELECT count(*) FROM opportunities WHERE vertical = 'CHILDRENS_HOME'
                        AND publication_status = 'PUBLISHED'
                        AND publication_automation_provenance ? 'policy_version'),
                      (SELECT count(*) FROM opportunities WHERE vertical = 'CHILDRENS_HOME'
                        AND publication_status = 'PUBLISHED'
                        AND NOT (publication_automation_provenance ? 'policy_version'))
               FROM care_publication_automation_state WHERE singleton"""
        ).fetchone()
        publication_window_rows = conn.execute(
            """WITH windows(label, since_at) AS (
                 VALUES ('24h', now() - interval '24 hours'),
                        ('7d', now() - interval '7 days'),
                        ('30d', now() - interval '30 days')
               )
               SELECT w.label,
                      count(*) FILTER (WHERE i.status = 'PUBLISHED'),
                      count(*) FILTER (WHERE i.status = 'SKIPPED'),
                      count(*) FILTER (WHERE i.status = 'FAILED')
               FROM windows w
               LEFT JOIN care_publication_run_items i ON i.created_at >= w.since_at
               GROUP BY w.label"""
        ).fetchall()
        recent_publication_runs = conn.execute(
            """SELECT id, status, selected_count, published_count, skipped_count,
                      failed_count, started_at, completed_at, policy_version, trigger_source
               FROM care_publication_runs ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()
        recent_publication_failures = conn.execute(
            """SELECT opportunity_id, reason, created_at
               FROM care_publication_run_items WHERE status = 'FAILED'
               ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()
        withdrawal_state = conn.execute(
            """SELECT execution_enabled, recurring_enabled, policy_version,
                      emergency_reason, max_withdrawals_per_execution,
                      last_execution_at, last_selected, last_withdrawn,
                      last_skipped, last_failed,
                      (SELECT count(*) FROM opportunities
                       WHERE vertical = 'CHILDRENS_HOME'
                         AND publication_status = 'WITHDRAWN'
                         AND withdrawal_automation_provenance ? 'policy_version')
               FROM care_withdrawal_automation_state WHERE singleton"""
        ).fetchone()
        withdrawal_window_rows = conn.execute(
            """WITH windows(label, since_at) AS (
                 VALUES ('24h', now() - interval '24 hours'),
                        ('7d', now() - interval '7 days'),
                        ('30d', now() - interval '30 days')
               )
               SELECT w.label,
                      count(*) FILTER (WHERE i.status = 'WITHDRAWN'),
                      count(*) FILTER (WHERE i.status = 'SKIPPED'),
                      count(*) FILTER (WHERE i.status = 'FAILED')
               FROM windows w
               LEFT JOIN care_withdrawal_run_items i ON i.created_at >= w.since_at
               GROUP BY w.label"""
        ).fetchall()
        recent_withdrawal_failures = conn.execute(
            """SELECT opportunity_id, reason, created_at
               FROM care_withdrawal_run_items WHERE status = 'FAILED'
               ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()

    signal_by_vertical: dict[str, Counter[str]] = {}
    signal_status = Counter()
    for vertical, status, count in signal_rows:
        signal_by_vertical.setdefault(str(vertical), Counter())[str(status)] += int(count)
        signal_status[str(status)] += int(count)
    unmatched_by_vertical = _counts(unmatched_rows, 0, 1)
    active_by_vertical = _counts(opportunity_rows, 0, 1)
    match_by_vertical = _counts(match_rows, 0, 1)

    transitions: dict[str, Any] = {}
    for label in WINDOWS:
        values = {
            f"{old_state}->{new_state}": int(count)
            for window, old_state, new_state, count in transition_rows
            if window == label
        }
        transitions[label] = {"total": sum(values.values()), "by_transition": values}

    cadence = {f"{int(days)}_days": int(count) for days, count in cadence_rows if days}
    projected_daily, projected_monthly = project_planning_watch_requests(Counter(cadence))
    requests_today, requests_month, successful, unchanged, changed, failed = map(
        int, watcher_run_row or (0, 0, 0, 0, 0, 0)
    )
    watcher_attempts = successful + failed

    publication_windows = {
        label: {
            "published": int(published or 0),
            "skipped": int(skipped or 0),
            "failed": int(failures or 0),
            "success_rate": _rate(int(published or 0), int(published or 0) + int(failures or 0)),
        }
        for label, published, skipped, failures in publication_window_rows
    }
    withdrawal_windows = {
        label: {
            "withdrawn": int(withdrawn or 0),
            "skipped": int(skipped or 0),
            "failed": int(failures or 0),
            "success_rate": _rate(
                int(withdrawn or 0), int(withdrawn or 0) + int(failures or 0)
            ),
        }
        for label, withdrawn, skipped, failures in withdrawal_window_rows
    }
    withdrawal_windows = {
        label: withdrawal_windows.get(
            label,
            {"withdrawn": 0, "skipped": 0, "failed": 0, "success_rate": _rate(0, 0)},
        )
        for label in WINDOWS
    }
    publication_windows = {
        label: publication_windows.get(
            label,
            {"published": 0, "skipped": 0, "failed": 0, "success_rate": _rate(0, 0)},
        )
        for label in WINDOWS
    }

    missing_org = sum(not item["opportunity"].get("operator_name") for item in decisions)
    missing_site = sum(
        not (item["opportunity"].get("town") or item["opportunity"].get("postcode"))
        for item in decisions
    )
    manual_blocks = sum(
        bool(item["opportunity"].get("publication_automation_blocked")) for item in decisions
    )
    publication_conflicts = 0
    for item in decisions:
        opportunity = item["opportunity"]
        if opportunity.get("publication_status") != "PUBLISHED":
            continue
        hygiene = item["hygiene"]
        underlying_category = hygiene["category"]
        if (
            underlying_category == "MANUAL_OR_ADMIN_TOUCHED_PRESERVE"
            and set(hygiene.get("admin_touch_types") or []) <= {"manual_publication"}
            and int(hygiene.get("foundational_signal_count") or 0) > 0
        ):
            underlying_category = "VALID_SUPPORTED"
        underlying = evaluate_publication(
            item["projection"],
            str(opportunity.get("customer_lifecycle_stage") or "NEEDS_REVIEW"),
            opportunity.get("relationships") or [],
            hygiene_category=underlying_category,
            hygiene_warning=hygiene.get("warning"),
            safe_title=item["safe_title"],
            safe_summary=item["safe_summary"],
            respect_existing_publication=False,
        )
        publication_conflicts += underlying.outcome not in {
            "AUTO_PUBLISH_ELIGIBLE",
            "QA_HOLDOUT",
        }
    attention_count = sum(
        hygiene_counts[name]
        for name in (
            "NEEDS_INVESTIGATION",
            "UNSUPPORTED_ORPHAN_CANDIDATE",
            "DUPLICATE_CANDIDATE",
            "SUPERSEDED_CANDIDATE",
        )
    )

    queue_unavailable = _unavailable(
        "Queue depth and oldest-message age are not persisted or cached; "
        "this endpoint does not call AWS."
    )
    queues = {
        "available": False,
        "reason": queue_unavailable["reason"],
        "items": [
            {"name": name, "depth": None, "oldest_message_age_seconds": None, "available": False}
            for name in (
                "ingestion",
                "ingestion_dlq",
                "enrichment",
                "enrichment_dlq",
                "planning_manual",
                "planning_manual_dlq",
            )
        ],
        "historical_baseline_dlq_messages": None,
    }
    collector_unavailable = _unavailable(
        "Collector run history is stored in DynamoDB and is not synchronously queried "
        "by this endpoint."
    )

    return {
        "schema_version": OPERATIONS_SUMMARY_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": settings.environment,
        "read_only": True,
        "definitions": {
            "time_windows": "Exact rolling UTC windows based on persisted event timestamps.",
            "active_opportunity": (
                "Review state is not MERGED/REJECTED and customer lifecycle is not STOPPED."
            ),
            "unmatched_signal": (
                "Approved actionable signal without an ACTIVE opportunity relationship."
            ),
        },
        "ingestion": {
            "windows": _window_payload(ingestion_rows),
            "failure_metric": _unavailable(
                "Collector failure history is not stored in PostgreSQL; "
                "see recent_executions.collectors."
            ),
        },
        "signals": {
            "total": sum(signal_status.values()),
            "by_review_status": dict(sorted(signal_status.items())),
            "by_vertical": {
                key: dict(sorted(value.items()))
                for key, value in sorted(signal_by_vertical.items())
            },
            "unmatched": sum(unmatched_by_vertical.values()),
            "unmatched_by_vertical": unmatched_by_vertical,
            "stale_pending": {
                "count": None,
                "available": False,
                "reason": "No canonical stale-review threshold is defined.",
            },
            "review_rates": {
                "acceptance": _rate(
                    signal_status["APPROVED"], signal_status["APPROVED"] + signal_status["REJECTED"]
                ),
                "rejection": _rate(
                    signal_status["REJECTED"], signal_status["APPROVED"] + signal_status["REJECTED"]
                ),
            },
        },
        "opportunities": {
            "active_total": sum(active_by_vertical.values()),
            "active_by_vertical": active_by_vertical,
            "unmatched_signals": sum(unmatched_by_vertical.values()),
            "match_review_backlog": sum(match_by_vertical.values()),
            "match_review_by_vertical": match_by_vertical,
            "needs_attention": attention_count,
            "duplicates": hygiene_counts["DUPLICATE_CANDIDATE"],
            "superseded": hygiene_counts["SUPERSEDED_CANDIDATE"],
            "recent_activity": {
                label: {
                    "created": int(created),
                    "relationship_link_events": int(linked),
                    "merge_audit_events": int(merged),
                }
                for label, created, linked, merged in recent_activity_rows
            },
            "signal_to_opportunity_conversion": {
                "available": False,
                "reason": "Raw signal and opportunity totals are not comparable cohorts.",
            },
        },
        "lifecycle": {
            "vertical": "CHILDRENS_HOME",
            "current": _counts(lifecycle_rows, 0, 1),
            "transitions": transitions,
        },
        "planning_watcher": {
            "enabled": bool(watcher_row and watcher_row[0]),
            "schedule_enabled": settings.care_lifecycle_watcher_schedule_enabled,
            "policy_version": watcher_row[1]
            if watcher_row
            else CARE_PLANNING_WATCHER_POLICY_VERSION,
            "emergency_reason": watcher_row[2] if watcher_row else None,
            "persisted_watches": int(watcher_row[6] or 0) if watcher_row else 0,
            "enabled_watches": int(watcher_row[7] or 0) if watcher_row else 0,
            "due_now": int(watcher_row[8] or 0) if watcher_row else 0,
            "cadence": cadence,
            "provider_requests_today": requests_today,
            "provider_requests_current_month": requests_month,
            "projected_requests_per_day": projected_daily,
            "projected_requests_per_30_days": projected_monthly,
            "limits": {
                "per_execution": int(watcher_row[3] or 0) if watcher_row else 0,
                "per_day": int(watcher_row[4] or 0) if watcher_row else 0,
                "per_month": int(watcher_row[5] or 0) if watcher_row else 0,
            },
            "polls": {
                "successful": successful,
                "unchanged": unchanged,
                "changed": changed,
                "failed": failed,
                "success_rate": _rate(successful, watcher_attempts),
                "change_rate": _rate(changed, successful),
                "failure_rate": _rate(failed, watcher_attempts),
            },
            "disabled_terminal": int(watcher_row[10] or 0) if watcher_row else 0,
            "recent_failures": [
                {
                    "run_id": str(row[0]),
                    "status": row[1],
                    "error_category": row[3],
                    "created_at": row[4],
                    "completed_at": row[5],
                }
                for row in recent_watcher_runs
                if row[1] in {"FAILED", "RATE_LIMITED", "QUEUE_FAILED"}
            ][:5],
            "next_due_poll_at": watcher_row[9] if watcher_row else None,
            "next_coordinator_run": None,
            "next_coordinator_run_available": False,
        },
        "publication": {
            "enabled": bool(publication_state and publication_state[0]),
            "recurring_enabled": bool(publication_state and publication_state[1]),
            "policy_version": publication_state[2]
            if publication_state
            else CARE_PUBLICATION_POLICY_VERSION,
            "emergency_reason": publication_state[3] if publication_state else None,
            "total_published": int((publication_state[10] or 0) + (publication_state[11] or 0))
            if publication_state
            else 0,
            "automatically_published": int(publication_state[10] or 0) if publication_state else 0,
            "manually_protected_published": int(publication_state[11] or 0)
            if publication_state
            else 0,
            "policy_outcomes": {
                "AUTO_PUBLISH_ELIGIBLE_UNPUBLISHED": len(inventory["eligible"]),
                "QA_HOLDOUT": publication_outcomes["QA_HOLDOUT"],
                "MANUAL_REVIEW": publication_outcomes["MANUAL_REVIEW"],
                "INELIGIBLE": publication_outcomes["INELIGIBLE"],
                "MANUAL_PROTECTION": publication_outcomes["MANUAL_PROTECTION"],
                "ALREADY_PUBLISHED": publication_outcomes["ALREADY_PUBLISHED"],
            },
            "published_policy_conflicts": publication_conflicts,
            "windows": publication_windows,
            "last_execution_at": publication_state[5] if publication_state else None,
            "latest_execution": {
                "selected": int(publication_state[6] or 0) if publication_state else 0,
                "published": int(publication_state[7] or 0) if publication_state else 0,
                "skipped": int(publication_state[8] or 0) if publication_state else 0,
                "failed": int(publication_state[9] or 0) if publication_state else 0,
            },
            "recent_failures": [
                {"opportunity_id": str(row[0]), "reason": row[1], "created_at": row[2]}
                for row in recent_publication_failures
            ],
            "next_coordinator_run": None,
            "next_coordinator_run_available": False,
        },
        "withdrawal": {
            **withdrawal_preview,
            "preview_only": not bool(
                withdrawal_state and withdrawal_state[0] and withdrawal_state[1]
            ),
            "enabled": bool(withdrawal_state and withdrawal_state[0]),
            "recurring_enabled": bool(withdrawal_state and withdrawal_state[1]),
            "policy_version": (
                withdrawal_state[2]
                if withdrawal_state
                else CARE_WITHDRAWAL_POLICY_VERSION
            ),
            "emergency_reason": withdrawal_state[3] if withdrawal_state else None,
            "max_withdrawals_per_execution": (
                int(withdrawal_state[4]) if withdrawal_state else 10
            ),
            "total_automatic_withdrawals": (
                int(withdrawal_state[10] or 0) if withdrawal_state else 0
            ),
            "last_execution_at": withdrawal_state[5] if withdrawal_state else None,
            "latest_execution": {
                "selected": int(withdrawal_state[6] or 0) if withdrawal_state else 0,
                "withdrawn": int(withdrawal_state[7] or 0) if withdrawal_state else 0,
                "skipped": int(withdrawal_state[8] or 0) if withdrawal_state else 0,
                "failed": int(withdrawal_state[9] or 0) if withdrawal_state else 0,
            },
            "windows": withdrawal_windows,
            "recent_failures": [
                {"opportunity_id": str(row[0]), "reason": row[1], "created_at": row[2]}
                for row in recent_withdrawal_failures
            ],
            "next_coordinator_run": None,
            "next_coordinator_run_available": False,
            "phase": "CONTROLLED_RUNTIME",
            "message": (
                "Automatic withdrawal is enabled with bounded execution."
                if withdrawal_state and withdrawal_state[0] and withdrawal_state[1]
                else "Automatic withdrawal is disabled."
            ),
        },
        "queues": queues,
        "data_quality": {
            "hygiene_categories": dict(sorted(hygiene_counts.items())),
            "needs_review_lifecycle": _counts(lifecycle_rows, 0, 1).get("NEEDS_REVIEW", 0),
            "missing_organisation_identity": missing_org,
            "missing_site_or_location_identity": missing_site,
            "duplicate_or_superseded": hygiene_counts["DUPLICATE_CANDIDATE"]
            + hygiene_counts["SUPERSEDED_CANDIDATE"],
            "manual_automation_blocks": manual_blocks,
            "publication_conflicts": publication_conflicts,
            "unmatched_strong_signals": sum(unmatched_by_vertical.values()),
        },
        "recent_executions": {
            "planning_watcher": [
                {
                    "id": str(row[0]),
                    "status": row[1],
                    "provider_requests": int(row[2] or 0),
                    "error_category": row[3],
                    "started_at": row[4],
                    "completed_at": row[5],
                    "policy_version": row[6] or CARE_PLANNING_WATCHER_POLICY_VERSION,
                }
                for row in recent_watcher_runs
            ],
            "automatic_publication": [
                {
                    "id": str(row[0]),
                    "status": row[1],
                    "selected": int(row[2] or 0),
                    "succeeded": int(row[3] or 0),
                    "skipped": int(row[4] or 0),
                    "failed": int(row[5] or 0),
                    "started_at": row[6],
                    "completed_at": row[7],
                    "policy_version": row[8],
                    "trigger_source": row[9],
                }
                for row in recent_publication_runs
            ],
            "collectors": {**collector_unavailable, "items": []},
        },
        "health": {
            "queues_ok": None,
            "queues_ok_metric": queues,
            "watcher_ok": bool(watcher_row and watcher_row[0]) and failed == 0,
            "watcher_ok_metric": {
                "enabled": bool(watcher_row and watcher_row[0]),
                "failed_polls": failed,
            },
            "publication_ok": bool(publication_state and publication_state[0])
            and int(publication_state[9] or 0) == 0,
            "publication_ok_metric": {
                "enabled": bool(publication_state and publication_state[0]),
                "last_failed": int(publication_state[9] or 0) if publication_state else 0,
            },
            "withdrawal_ok": bool(withdrawal_state and withdrawal_state[0])
            and int(withdrawal_state[9] or 0) == 0,
            "withdrawal_ok_metric": {
                "enabled": bool(withdrawal_state and withdrawal_state[0]),
                "last_failed": int(withdrawal_state[9] or 0) if withdrawal_state else 0,
            },
            "provider_quota_ok": bool(watcher_row)
            and requests_today < int(watcher_row[4])
            and requests_month < int(watcher_row[5]),
            "provider_quota_metric": {
                "today": requests_today,
                "daily_limit": int(watcher_row[4] or 0) if watcher_row else 0,
                "month": requests_month,
                "monthly_limit": int(watcher_row[5] or 0) if watcher_row else 0,
            },
            "recent_failures_present": bool(
                failed or recent_publication_failures or recent_withdrawal_failures
            ),
        },
    }
