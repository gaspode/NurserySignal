from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

from app.repository import (
    execute_care_withdrawal_batch,
    set_care_withdrawal_automation_execution,
)


class Result:
    def __init__(self, *, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class WithdrawalConnection:
    def __init__(self, *, enabled=True, recurring=False, fail_ids=()):
        self.enabled = enabled
        self.recurring = recurring
        self.fail_ids = set(fail_ids)
        self.run_id = UUID("00000000-0000-0000-0000-000000000902")
        self.withdrawn = []
        self.run_items = []
        self.audit = []
        self.statements = []

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if normalized.startswith("SELECT execution_enabled"):
            return Result(
                one=(self.enabled, self.recurring, 10, "care-withdrawal-v1", None)
            )
        if normalized.startswith("INSERT INTO care_withdrawal_runs"):
            return Result(one=(self.run_id,))
        if normalized.startswith("UPDATE opportunities"):
            opportunity_id = str(params[-1])
            if opportunity_id in self.fail_ids:
                raise RuntimeError("isolated write failure")
            self.withdrawn.append((opportunity_id, params))
            return Result(one=(UUID(opportunity_id), "WITHDRAWN", "2026-10-01T14:00:00Z"))
        if normalized.startswith("INSERT INTO care_withdrawal_run_items"):
            self.run_items.append(params)
            return Result()
        if normalized.startswith("INSERT INTO admin_audit_events"):
            self.audit.append(params)
            return Result()
        if normalized.startswith("UPDATE care_withdrawal_runs"):
            return Result()
        if normalized.startswith("UPDATE care_withdrawal_automation_state"):
            if "RETURNING execution_enabled" in normalized:
                self.enabled = bool(params[0])
                self.recurring = bool(params[1])
                return Result(
                    one=(
                        self.enabled,
                        self.recurring,
                        params[2],
                        10,
                        "care-withdrawal-v1",
                        "2026-10-01T14:00:00Z",
                    )
                )
            return Result()
        raise AssertionError(normalized)

    def commit(self):
        pass

    def rollback(self):
        pass


def candidate(index: int) -> dict:
    opportunity_id = UUID(f"00000000-0000-0000-0000-{index:012d}")
    signal = {
        "id": f"signal-{index}",
        "source_type": "planning",
        "status": "ACTIVE",
        "metadata": {"decision": "Refused"},
    }
    opportunity = {
        "id": opportunity_id,
        "vertical": "CHILDRENS_HOME",
        "publication_status": "PUBLISHED",
        "customer_lifecycle_stage": "STOPPED",
        "publication_automation_blocked": False,
        "publication_automation_provenance": {"policy_version": "care-publication-v3"},
        "relationships": [signal],
    }
    return {
        "opportunity": opportunity,
        "projection": opportunity,
        "hygiene": {"category": "VALID_SUPPORTED", "warning": None},
        "withdrawal_decision": SimpleNamespace(
            outcome="AUTO_WITHDRAW_ELIGIBLE", reason="planning_refused", exclusions=()
        ),
    }


def withdrawal_inventory(items):
    return {
        "published": list(items),
        "eligible": list(items),
        "selectable": list(items),
        "deferred_failures": [],
        "outcomes": {"AUTO_WITHDRAW_ELIGIBLE": len(items)},
    }


def install_runtime(
    monkeypatch, fake, items, *, current_outcome="AUTO_WITHDRAW_ELIGIBLE"
):
    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr(
        "app.repository._current_care_withdrawal_inventory",
        lambda settings: withdrawal_inventory(items),
    )
    by_id = {str(item["opportunity"]["id"]): item["opportunity"] for item in items}
    monkeypatch.setattr(
        "app.repository._care_policy_opportunities",
        lambda conn, opportunity_id=None: [by_id[opportunity_id]],
    )
    monkeypatch.setattr(
        "app.repository.audit_opportunities",
        lambda opportunities: {
            "items": [
                {
                    "opportunity_id": str(opportunities[0]["id"]),
                    "category": "VALID_SUPPORTED",
                    "warning": None,
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.repository._care_publication_projection",
        lambda opportunity, hygiene: (opportunity, "safe title", "safe summary", None),
    )
    monkeypatch.setattr(
        "app.repository._care_withdrawal_decision",
        lambda item: SimpleNamespace(
            outcome=current_outcome,
            reason="planning_refused" if current_outcome == "AUTO_WITHDRAW_ELIGIBLE" else "changed",
            exclusions=(),
        ),
    )


def test_zero_candidate_execution_is_a_bounded_noop(monkeypatch) -> None:
    fake = WithdrawalConnection()
    install_runtime(monkeypatch, fake, [])
    result = execute_care_withdrawal_batch(object(), actor="admin", limit=10)
    assert result["selected"] == result["withdrawn"] == result["failed"] == 0
    assert result["publication_changes"] == result["withdrawal_changes"] == 0
    assert fake.withdrawn == []


def test_batch_is_bounded_audited_and_persists_provenance(monkeypatch) -> None:
    items = [candidate(index) for index in range(1, 16)]
    fake = WithdrawalConnection()
    install_runtime(monkeypatch, fake, items)
    result = execute_care_withdrawal_batch(object(), actor="admin", limit=50)
    assert result["selected"] == result["withdrawn"] == 10
    assert result["skipped"] == result["failed"] == 0
    assert result["audit_rows_created"] == 10
    assert len(fake.withdrawn) == len(fake.audit) == 10
    audit_statements = [
        statement for statement in fake.statements if "admin_audit_events" in statement
    ]
    assert all("care_opportunity_auto_withdrawn" in statement for statement in audit_statements)
    assert not any(
        "SET publication_status = 'PUBLISHED'" in statement
        for statement in fake.statements
    )


def test_policy_is_rechecked_before_mutation(monkeypatch) -> None:
    items = [candidate(1)]
    fake = WithdrawalConnection()
    install_runtime(monkeypatch, fake, items, current_outcome="KEEP_PUBLISHED")
    result = execute_care_withdrawal_batch(object(), actor="admin", limit=1)
    assert result["withdrawn"] == 0
    assert result["skipped"] == result["unexpected_policy_transitions"] == 1
    assert fake.withdrawn == []


def test_rerun_after_success_is_idempotent(monkeypatch) -> None:
    items = [candidate(1)]
    fake = WithdrawalConnection()
    install_runtime(monkeypatch, fake, items)
    first = execute_care_withdrawal_batch(object(), actor="admin", limit=1)
    monkeypatch.setattr(
        "app.repository._current_care_withdrawal_inventory",
        lambda settings: withdrawal_inventory([]),
    )
    second = execute_care_withdrawal_batch(object(), actor="admin", limit=1)
    assert first["withdrawn"] == 1
    assert second["selected"] == second["withdrawn"] == 0
    assert len(fake.withdrawn) == 1


def test_failure_is_isolated(monkeypatch) -> None:
    items = [candidate(1), candidate(2)]
    failed_id = str(items[0]["opportunity"]["id"])
    fake = WithdrawalConnection(fail_ids={failed_id})
    install_runtime(monkeypatch, fake, items)
    result = execute_care_withdrawal_batch(object(), actor="admin", limit=2)
    assert result["selected"] == 2
    assert result["failed"] == 1
    assert result["withdrawn"] == 1
    assert result["failures"] == [{"opportunity_id": failed_id, "error": "RuntimeError"}]


def test_disabled_or_nonrecurring_switch_prevents_execution(monkeypatch) -> None:
    for fake, require_recurring in (
        (WithdrawalConnection(enabled=False), False),
        (WithdrawalConnection(enabled=True, recurring=False), True),
    ):
        @contextmanager
        def fake_connection(settings):
            yield fake

        monkeypatch.setattr("app.repository.connection", fake_connection)
        result = execute_care_withdrawal_batch(
            object(), actor="system", require_recurring=require_recurring
        )
        assert result["withdrawn"] == result["withdrawal_changes"] == 0
        assert result["publication_changes"] == 0


def test_emergency_disable_preserves_publications_and_is_audited(monkeypatch) -> None:
    fake = WithdrawalConnection(enabled=True, recurring=True)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = set_care_withdrawal_automation_execution(
        object(),
        enabled=False,
        recurring_enabled=False,
        actor="admin",
        reason="emergency stop",
    )
    assert result["execution_enabled"] is False
    assert result["recurring_enabled"] is False
    assert result["publication_changes"] == result["withdrawal_changes"] == 0
    assert fake.withdrawn == []
    assert len(fake.audit) == 1
