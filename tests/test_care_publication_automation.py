from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

from app.repository import (
    care_publication_automation_preview,
    execute_care_publication_batch,
    set_care_publication_automation_execution,
)


class Result:
    def __init__(self, *, one=None, rows=None):
        self.one = one
        self.rows = rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class PublicationConnection:
    def __init__(self, *, enabled=True, recurring=False, fail_ids=()):
        self.enabled = enabled
        self.recurring = recurring
        self.fail_ids = set(fail_ids)
        self.run_id = UUID("00000000-0000-0000-0000-000000000901")
        self.published = []
        self.run_items = []
        self.audit = []
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if normalized.startswith("SELECT execution_enabled"):
            return Result(
                one=(self.enabled, self.recurring, 25, "care-publication-v3", None)
            )
        if normalized.startswith("INSERT INTO care_publication_runs"):
            return Result(one=(self.run_id,))
        if normalized.startswith("UPDATE opportunities"):
            opportunity_id = str(params[-1])
            if opportunity_id in self.fail_ids:
                raise RuntimeError("isolated write failure")
            self.published.append((opportunity_id, params))
            return Result(
                one=(
                    UUID(opportunity_id),
                    "PUBLISHED",
                    params[0],
                    params[1],
                    "2026-10-01T12:00:00Z",
                )
            )
        if normalized.startswith("INSERT INTO care_publication_run_items"):
            self.run_items.append(params)
            return Result()
        if normalized.startswith("INSERT INTO admin_audit_events"):
            self.audit.append(params)
            return Result()
        if normalized.startswith("UPDATE care_publication_runs"):
            return Result()
        if normalized.startswith("UPDATE care_publication_automation_state"):
            if "RETURNING execution_enabled" in normalized:
                self.enabled = bool(params[0])
                self.recurring = bool(params[1])
                return Result(
                    one=(
                        self.enabled,
                        self.recurring,
                        params[2],
                        25,
                        "care-publication-v3",
                        "2026-10-01T12:00:00Z",
                    )
                )
            return Result()
        raise AssertionError(normalized)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def candidate(index: int) -> dict:
    opportunity_id = UUID(f"00000000-0000-0000-0000-{index:012d}")
    opportunity = {
        "id": opportunity_id,
        "vertical": "CHILDRENS_HOME",
        "publication_status": "DRAFT",
        "customer_lifecycle_stage": "PLANNING_APPROVED",
        "relationships": [],
    }
    return {
        "opportunity": opportunity,
        "projection": opportunity,
        "hygiene": {"category": "VALID_SUPPORTED"},
        "safe_title": "New children's home — Nottingham, NG8",
        "safe_summary": "Planning permission has been approved.",
        "decision": SimpleNamespace(
            outcome="AUTO_PUBLISH_ELIGIBLE", reason="eligible", exclusions=()
        ),
    }


def inventory(items):
    return {
        "opportunities": [item["opportunity"] for item in items],
        "outcomes": {"AUTO_PUBLISH_ELIGIBLE": len(items)},
        "eligible": list(items),
        "selectable": list(items),
        "deferred_failures": [],
    }


def install_runtime(monkeypatch, fake, items, *, current_outcome="AUTO_PUBLISH_ELIGIBLE"):
    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr(
        "app.repository._current_care_publication_inventory", lambda settings: inventory(items)
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
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.repository._care_publication_projection",
        lambda opportunity, hygiene: (
            opportunity,
            "New children's home — Nottingham, NG8",
            "Planning permission has been approved.",
            SimpleNamespace(outcome=current_outcome, reason="current_policy", exclusions=()),
        ),
    )


def test_preview_is_bounded_and_read_only(monkeypatch) -> None:
    items = [candidate(index) for index in range(1, 31)]
    monkeypatch.setattr(
        "app.repository._current_care_publication_inventory", lambda settings: inventory(items)
    )
    result = care_publication_automation_preview(object(), limit=999)
    assert result["selected"] == 30
    assert result["eligible_unpublished"] == 30
    assert result["publication_mutations"] == result["withdrawal_mutations"] == 0


def test_batch_is_bounded_audited_and_records_automatic_provenance(monkeypatch) -> None:
    items = [candidate(index) for index in range(1, 31)]
    fake = PublicationConnection()
    install_runtime(monkeypatch, fake, items)
    result = execute_care_publication_batch(object(), actor="admin", limit=10)
    assert result["selected"] == result["published"] == 10
    assert result["skipped"] == result["failed"] == 0
    assert result["audit_rows_created"] == 10
    assert result["withdrawal_changes"] == 0
    assert len(fake.published) == len(fake.audit) == 10
    assert all(params[2] == "SYSTEM_PUBLICATION_COORDINATOR" for _, params in fake.published)
    audit_statements = [
        statement for statement in fake.statements if "admin_audit_events" in statement
    ]
    assert all("care_opportunity_auto_published" in statement for statement in audit_statements)
    assert not any("WITHDRAWN" in statement for statement in fake.statements)


def test_policy_is_rechecked_immediately_and_state_change_is_skipped(monkeypatch) -> None:
    items = [candidate(1)]
    fake = PublicationConnection()
    install_runtime(monkeypatch, fake, items, current_outcome="MANUAL_REVIEW")
    result = execute_care_publication_batch(object(), actor="admin", limit=1)
    assert result["published"] == 0
    assert result["skipped"] == 1
    assert result["unexpected_policy_transitions"] == 1
    assert fake.published == []


def test_batch_failure_is_isolated_and_does_not_publish_failed_record(monkeypatch) -> None:
    items = [candidate(1), candidate(2)]
    failed_id = str(items[0]["opportunity"]["id"])
    fake = PublicationConnection(fail_ids={failed_id})
    install_runtime(monkeypatch, fake, items)
    result = execute_care_publication_batch(object(), actor="admin", limit=2)
    assert result["selected"] == 2
    assert result["failed"] == 1
    assert result["published"] == 1
    assert result["failures"] == [{"opportunity_id": failed_id, "error": "RuntimeError"}]


def test_disabled_or_nonrecurring_switch_prevents_execution(monkeypatch) -> None:
    for fake, require_recurring in (
        (PublicationConnection(enabled=False), False),
        (PublicationConnection(enabled=True, recurring=False), True),
    ):
        @contextmanager
        def fake_connection(settings):
            yield fake

        monkeypatch.setattr("app.repository.connection", fake_connection)
        result = execute_care_publication_batch(
            object(), actor="system", require_recurring=require_recurring
        )
        assert result["published"] == result["publication_changes"] == 0
        assert result["withdrawal_changes"] == 0
        assert fake.published == []


def test_emergency_disable_is_audited_and_preserves_publications(monkeypatch) -> None:
    fake = PublicationConnection(enabled=True, recurring=True)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = set_care_publication_automation_execution(
        object(),
        enabled=False,
        recurring_enabled=False,
        actor="admin",
        reason="emergency stop",
    )
    assert result["execution_enabled"] is False
    assert result["recurring_enabled"] is False
    assert result["publication_changes"] == result["withdrawal_changes"] == 0
    assert fake.published == []
    assert len(fake.audit) == 1
