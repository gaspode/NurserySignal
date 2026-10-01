from contextlib import contextmanager
from uuid import UUID

from app.repository import bootstrap_care_opportunity_lifecycles


class Result:
    def __init__(self, *, one=None, all_rows=None):
        self.one = one
        self.all_rows = all_rows or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class BootstrapConnection:
    def __init__(self, rows, *, before=(2, 0), remaining=1, fail_id=None):
        self.rows = rows
        self.before = before
        self.remaining = remaining
        self.fail_id = fail_id
        self.history = []
        self.updates = []
        self.audit = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement, params=None):
        normalized = " ".join(statement.split())
        if "count(*) FILTER" in normalized:
            return Result(one=self.before)
        if normalized.startswith("SELECT o.id"):
            return Result(all_rows=self.rows)
        if normalized.startswith("UPDATE opportunities"):
            opportunity_id = str(params[3])
            if opportunity_id == self.fail_id:
                raise RuntimeError("isolated database failure")
            self.updates.append(params)
            return Result(one=(UUID(opportunity_id),))
        if normalized.startswith("INSERT INTO opportunity_lifecycle_history"):
            self.history.append(params)
            return Result()
        if normalized.startswith("SELECT count(*) FROM opportunities"):
            return Result(one=(self.remaining,))
        if normalized.startswith("INSERT INTO admin_audit_events"):
            self.audit.append(params)
            return Result()
        raise AssertionError(normalized)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def planning_signal(signal_id, outcome):
    return {
        "id": signal_id,
        "source_type": "planning",
        "status": "ACTIVE",
        "relationship_status": "ACTIVE",
        "review_status": "APPROVED",
        "metadata": {"decision": outcome},
        "extracted_facts": {
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
            "opportunity_creation_decision": "CREATE_OPPORTUNITY",
        },
        "relationship_extracted_facts": {},
        "planning_family_relationship_types": [],
    }


def test_repository_bootstrap_persists_versioned_history_and_isolates_errors(monkeypatch):
    opportunity_one = UUID("00000000-0000-0000-0000-000000000101")
    opportunity_two = UUID("00000000-0000-0000-0000-000000000102")
    rows = [
        (
            opportunity_one,
            None,
            [planning_signal("00000000-0000-0000-0000-000000000201", "Pending")],
        ),
        (
            opportunity_two,
            None,
            [planning_signal("00000000-0000-0000-0000-000000000202", "Granted")],
        ),
    ]
    fake = BootstrapConnection(rows, fail_id=str(opportunity_two))

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = bootstrap_care_opportunity_lifecycles(
        object(), actor="bootstrap-admin", limit=999, preview=False
    )

    assert result["batch_limit"] == 100
    assert result["examined"] == 2
    assert result["persisted"] == 1
    assert result["failed"] == 1
    assert result["history_rows_created"] == 1
    assert result["remaining_unset"] == 1
    assert result["provider_requests"] == 0
    assert result["publication_changes"] == result["withdrawal_changes"] == 0
    assert fake.rollbacks == 1
    assert len(fake.history) == 1
    history = fake.history[0]
    assert history[1] == "PLANNING_PENDING"
    assert history[5] == "care-opportunity-lifecycle-v1"
    assert history[6] == "bootstrap-admin"
    assert len(fake.audit) == 1


def test_repository_bootstrap_rerun_with_no_unset_rows_writes_nothing(monkeypatch):
    fake = BootstrapConnection([], before=(0, 1145), remaining=0)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = bootstrap_care_opportunity_lifecycles(
        object(), actor="bootstrap-admin", limit=100, preview=False
    )
    assert result["examined"] == result["persisted"] == result["failed"] == 0
    assert result["already_populated_total"] == 1145
    assert fake.history == fake.audit == []
    assert fake.commits == fake.rollbacks == 0
