from contextlib import contextmanager
from types import SimpleNamespace

from app.config import Settings
from app.operations import OPERATIONS_SUMMARY_SCHEMA_VERSION, _rate, operations_summary


class Result:
    def __init__(self, *, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class ReadOnlyConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        sql = " ".join(statement.split())
        self.statements.append(sql)
        if "JOIN raw_signals rs ON rs.discovered_at" in sql:
            return Result(rows=[("24h", "CHILDRENS_HOME", "planning", 4, 3, 1, 0)])
        if "GROUP BY rs.vertical, COALESCE(se.review_status" in sql:
            return Result(rows=[("CHILDRENS_HOME", "APPROVED", 8), ("NURSERY", "PENDING", 2)])
        if "NOT EXISTS ( SELECT 1 FROM opportunity_signals" in sql:
            return Result(rows=[("CHILDRENS_HOME", 2)])
        if "FROM opportunities WHERE review_status NOT IN" in sql:
            return Result(rows=[("CHILDRENS_HOME", 1)])
        if "FROM opportunity_match_reviews" in sql:
            return Result(rows=[("CHILDRENS_HOME", 1)])
        if (
            "COALESCE(customer_lifecycle_stage, 'UNSET')" in sql
            and "opportunity_lifecycle_history" not in sql
        ):
            return Result(rows=[("PLANNING_APPROVED", 1)])
        if "JOIN opportunity_lifecycle_history" in sql:
            return Result(rows=[("24h", "PLANNING_PENDING", "PLANNING_APPROVED", 1)])
        if "SELECT w.label, (SELECT count(*) FROM opportunities" in sql:
            return Result(rows=[("24h", 1, 1, 0), ("7d", 1, 1, 0), ("30d", 1, 1, 0)])
        if "FROM planning_lifecycle_watcher_state s" in sql:
            return Result(
                one=(
                    True,
                    "care-planning-watcher-v2",
                    None,
                    15,
                    50,
                    2000,
                    1,
                    1,
                    0,
                    "2026-10-02T00:00:00Z",
                    0,
                )
            )
        if "SELECT cadence_days, count(*)" in sql:
            return Result(rows=[(7, 1)])
        if "COALESCE(sum(provider_requests)" in sql:
            return Result(one=(1, 2, 2, 1, 1, 0))
        if "FROM planning_lifecycle_watch_runs ORDER BY" in sql:
            return Result(rows=[])
        if "FROM care_publication_automation_state" in sql:
            return Result(
                one=(
                    True,
                    True,
                    "care-publication-v3",
                    None,
                    25,
                    "2026-10-01T00:00:00Z",
                    1,
                    1,
                    0,
                    0,
                    1,
                    1,
                )
            )
        if "LEFT JOIN care_publication_run_items" in sql:
            return Result(rows=[("24h", 1, 0, 0), ("7d", 1, 0, 0), ("30d", 1, 0, 0)])
        if "FROM care_publication_runs ORDER BY" in sql:
            return Result(rows=[])
        if "FROM care_publication_run_items WHERE status = 'FAILED'" in sql:
            return Result(rows=[])
        raise AssertionError(sql)


def test_operations_summary_is_versioned_bounded_and_read_only(monkeypatch) -> None:
    fake = ReadOnlyConnection()

    @contextmanager
    def fake_connection(settings):
        yield fake

    opportunity = {
        "id": "opp-1",
        "operator_name": "Care Operator",
        "town": "Nottingham",
        "postcode": "NG8",
        "publication_status": "DRAFT",
        "publication_automation_blocked": False,
    }
    monkeypatch.setattr("app.operations.connection", fake_connection)
    monkeypatch.setattr(
        "app.operations._current_care_publication_inventory",
        lambda settings: {
            "opportunities": [opportunity],
            "decisions": [
                {
                    "opportunity": opportunity,
                    "hygiene": {"category": "VALID_SUPPORTED"},
                    "decision": SimpleNamespace(outcome="AUTO_PUBLISH_ELIGIBLE"),
                }
            ],
            "eligible": [{"opportunity": opportunity}],
        },
    )

    summary = operations_summary(
        Settings(environment="test", care_lifecycle_watcher_schedule_enabled=True)
    )

    assert summary["schema_version"] == OPERATIONS_SUMMARY_SCHEMA_VERSION
    assert summary["read_only"] is True
    assert summary["signals"]["by_review_status"] == {"APPROVED": 8, "PENDING": 2}
    assert summary["lifecycle"]["current"] == {"PLANNING_APPROVED": 1}
    assert summary["planning_watcher"]["polls"]["change_rate"]["rate_percent"] == 50.0
    assert summary["publication"]["windows"]["24h"]["success_rate"]["rate_percent"] == 100.0
    assert summary["queues"]["available"] is False
    assert summary["recent_executions"]["collectors"]["available"] is False
    assert summary["withdrawal"]["enabled"] is False
    assert all(statement.startswith(("SELECT", "WITH")) for statement in fake.statements)
    assert "secret" not in str(summary).lower()


def test_zero_denominators_and_missing_metrics_are_explicit() -> None:
    assert _rate(0, 0) == {"numerator": 0, "denominator": 0, "rate_percent": None}
