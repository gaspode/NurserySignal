from contextlib import contextmanager
from datetime import UTC, datetime

from app.config import Settings
from app.repository import (
    queue_due_care_planning_watches,
    set_care_planning_watcher_execution,
    update_care_planning_watch_result,
)


class Result:
    def __init__(self, *, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class StateConnection:
    def __init__(self, *, enabled=False, usage=(0, 0, 0)):
        self.enabled = enabled
        self.usage = usage
        self.commits = 0
        self.rollbacks = 0
        self.audit = []

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        if normalized.startswith("UPDATE planning_lifecycle_watcher_state"):
            self.enabled = bool(params[0])
            return Result(
                one=(
                    self.enabled,
                    params[1],
                    15,
                    50,
                    2000,
                    "care-planning-watcher-v2",
                    datetime(2026, 10, 1, tzinfo=UTC),
                )
            )
        if normalized.startswith("INSERT INTO admin_audit_events"):
            self.audit.append(params)
            return Result()
        if normalized.startswith("SELECT execution_enabled"):
            return Result(
                one=(self.enabled, None, 15, 50, 2000, "care-planning-watcher-v2")
            )
        if normalized.startswith("UPDATE planning_lifecycle_watch_runs"):
            return Result()
        if "COALESCE(sum(provider_requests)" in normalized:
            return Result(one=self.usage)
        raise AssertionError(normalized)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_emergency_disable_preserves_runtime_state_and_is_audited(monkeypatch) -> None:
    fake = StateConnection(enabled=True)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = set_care_planning_watcher_execution(
        object(), enabled=False, actor="admin", reason="provider anomaly"
    )
    assert result["execution_enabled"] is False
    assert result["publication_changes"] == result["withdrawal_changes"] == 0
    assert fake.commits == 1
    assert len(fake.audit) == 1


def test_coordinator_does_not_queue_when_execution_switch_is_off(monkeypatch) -> None:
    fake = StateConnection(enabled=False)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = queue_due_care_planning_watches(
        Settings(planning_manual_run_queue_url="https://sqs.example/planning"), max_due=15
    )
    assert result["execution_enabled"] is False
    assert result["provider_requests_queued"] == 0
    assert fake.rollbacks == 1


def test_daily_quota_guardrail_leaves_due_watches_unqueued(monkeypatch) -> None:
    fake = StateConnection(enabled=True, usage=(50, 400, 0))

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = queue_due_care_planning_watches(
        Settings(planning_manual_run_queue_url="https://sqs.example/planning"), max_due=100
    )
    assert result["reason"] == "quota_guardrail_reached"
    assert result["requests_today"] == 50
    assert result["provider_requests_queued"] == 0
    assert fake.rollbacks == 1


def test_due_watch_payload_includes_stable_plota_application_id(monkeypatch) -> None:
    sent = []

    class DueConnection(StateConnection):
        def execute(self, statement, params=None):
            normalized = " ".join(statement.split())
            if "FROM planning_lifecycle_watches w" in normalized:
                return Result(
                    rows=[
                        (
                            "watch-1",
                            "opportunity-1",
                            "signal-1",
                            "Croydon",
                            "24/03385/FUL",
                            {},
                            "58 Ashburton Road",
                            "CR0 6AN",
                            "plota:stable-123",
                        )
                    ]
                )
            if normalized.startswith("INSERT INTO planning_lifecycle_watch_runs"):
                return Result(one=("run-1",))
            return super().execute(statement, params)

    fake = DueConnection(enabled=True)

    @contextmanager
    def fake_connection(settings):
        yield fake

    class Sqs:
        def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"MessageId": "message-1"}

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr("app.repository.boto3.client", lambda service: Sqs())
    result = queue_due_care_planning_watches(
        Settings(planning_manual_run_queue_url="https://sqs.example/planning"), max_due=1
    )

    assert result["queued"] == 1
    assert '"provider_application_id":"stable-123"' in sent[0]["MessageBody"]


def test_watch_result_persists_stable_error_category(monkeypatch) -> None:
    captured = []

    class ResultConnection:
        def execute(self, statement, params=None):
            normalized = " ".join(statement.split())
            if normalized.startswith("SELECT status FROM planning_lifecycle_watch_runs"):
                return Result(one=("RUNNING",))
            if normalized.startswith("SELECT enabled, cadence_days"):
                return Result(one=(True, 7, 0))
            if normalized.startswith("UPDATE planning_lifecycle_watch_runs"):
                captured.append(params)
                return Result()
            if normalized.startswith("UPDATE planning_lifecycle_watches"):
                return Result()
            raise AssertionError(normalized)

        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def fake_connection(settings):
        yield ResultConnection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = update_care_planning_watch_result(
        object(),
        run_id="run-1",
        watch_id="watch-1",
        status="FAILED",
        details={
            "provider_requests": 1,
            "provider_result": "NOT_FOUND",
            "error_category": "provider_no_reference_match",
        },
    )

    assert result["status"] == "FAILED"
    assert captured[0][3] == "provider_no_reference_match"
