from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from app.config import Settings
from app.repository import reprocess_planning_signals


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.audit_inserts = 0
        self.operation_id = uuid4()
        self.last_result = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT rs.id" in sql:
            self.last_result = self.rows
        elif "INSERT INTO admin_audit_events" in sql:
            self.audit_inserts += 1
            self.last_result = [(self.operation_id,)]
        else:
            self.last_result = []
        return self

    def fetchall(self):
        return self.last_result

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self):
        return None


def stored_row(review_status: str, description: str, address: str = "12 High Street"):
    return (
        uuid4(),
        "1.0",
        "planning",
        "https://council.example/application/1",
        "plota:1",
        "2026-09-24T08:00:00+00:00",
        description,
        description,
        address,
        "Little Acorns Ltd",
        {
            "provider": "plota",
            "provider_application_id": "1",
            "provider_record": {
                "id": "1",
                "description": description,
                "address": address,
                "authority": {"name": "Bristol"},
                "date_received": "2026-09-24",
                "stage": "Pending Consideration",
                "links": {"council": "https://council.example/application/1"},
            },
        },
        review_status,
    )


def test_reprocess_is_bounded_preserves_review_state_and_writes_no_downstream_artifacts(
    monkeypatch,
):
    rows = [
        stored_row("PENDING", "Primary and nursery in a wider housing scheme"),
        stored_row("APPROVED", "Change of use to a children's day nursery"),
    ]
    fake = FakeConnection(rows)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = reprocess_planning_signals(
        Settings(), actor="admin-1", limit=2, discovered_from="2026-09-01"
    )

    assert result.selected == 2
    assert result.pending_updated == 1
    assert result.reviewed_preserved == 1
    assert result.matched == 1
    assert result.excluded == 1
    assert fake.audit_inserts == 1
    update_sql = [sql for sql, _ in fake.statements if "UPDATE signal_enrichments" in sql]
    assert len(update_sql) == 2
    assert "review_status = 'PENDING'" in update_sql[0]
    assert "SET extracted_facts = extracted_facts ||" in update_sql[1]
    assert all("review_status = %s" not in sql for sql in update_sql)


def test_reprocess_repeat_is_reclassification_not_ingestion(monkeypatch):
    rows = [stored_row("PENDING", "Change of use to a children's day nursery")]
    first_fake, second_fake = FakeConnection(rows), FakeConnection(rows)
    fakes = [first_fake, second_fake]

    @contextmanager
    def fake_connection(settings):
        yield fakes.pop(0)

    monkeypatch.setattr("app.repository.connection", fake_connection)
    first = reprocess_planning_signals(Settings(), actor="admin-1", limit=25)
    second = reprocess_planning_signals(Settings(), actor="admin-1", limit=25)

    assert first.selected == second.selected == 1
    assert first.pending_updated == second.pending_updated == 1
    # Each invocation has one legitimate operation audit row; neither run
    # creates an ingestion, evidence, or enrichment-queue operation.
    assert first_fake.audit_inserts == second_fake.audit_inserts == 1
