from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from app.config import Settings
from app.repository import backfill_historical_planning_family_metadata


class FamilyBackfillConnection:
    def __init__(self, selected_rows, candidate_rows):
        self.selected_rows = selected_rows
        self.candidate_rows = candidate_rows
        self.families = {}
        self.relationships = set()
        self.statements = []
        self.last_result = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "ORDER BY rs.created_at, rs.id LIMIT" in sql:
            self.last_result = self.selected_rows
        elif "SELECT rs.id, rs.external_id, rs.metadata" in sql:
            self.last_result = self.candidate_rows
        elif "SELECT id, origin_status FROM planning_application_families" in sql:
            family = self.families.get(tuple(params))
            self.last_result = [family] if family else []
        elif "SELECT 1 FROM planning_signal_family_relationships" in sql:
            self.last_result = [(1,)] if tuple(params) in self.relationships else []
        elif "INSERT INTO planning_application_families" in sql:
            key = (params[2], params[4])
            family = self.families.setdefault(key, (uuid4(), "MISSING"))
            self.last_result = [family]
        elif "INSERT INTO planning_signal_family_relationships" in sql:
            self.relationships.add((params[0], params[1]))
            self.last_result = []
        else:
            self.last_result = []
        return self

    def fetchall(self):
        return self.last_result

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def followup_row(*, authority="Croydon", references=None):
    return (
        uuid4(),
        "plota:followup-1",
        "Details pursuant to permission ref. 24/03385/FUL",
        "Discharge of condition 5 attached to planning permission 24/03385/FUL",
        {"council": authority, "planning_status": "approved"},
        {
            "planning_subtype": "CONDITION_DISCHARGE",
            "planning_prior_references": references or ["24/03385/FUL"],
            "opportunity_creation_decision": "SUPPORT_EXISTING_ONLY",
        },
    )


def test_historical_family_backfill_creates_missing_support_membership_idempotently(
    monkeypatch,
) -> None:
    row = followup_row()
    fake = FamilyBackfillConnection([row], [])

    @contextmanager
    def fake_connection(settings):
        yield fake

    audits = []
    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr(
        "app.repository.record_admin_audit", lambda *args, **kwargs: audits.append(kwargs)
    )

    preview = backfill_historical_planning_family_metadata(
        Settings(), actor="admin", preview=True
    )
    assert preview["missing_family_metadata"] == 1
    assert preview["families_created"] == 1
    assert preview["references_relationships_created"] == 1
    assert preview["origins_marked_missing"] == 1
    assert preview["plota_requests"] == preview["plota_records_consumed"] == 0
    assert not fake.families

    applied = backfill_historical_planning_family_metadata(
        Settings(), actor="admin", preview=False
    )
    repeated = backfill_historical_planning_family_metadata(
        Settings(), actor="admin", preview=False
    )
    assert applied["references_relationships_created"] == 1
    assert repeated["references_relationships_created"] == 0
    assert repeated["already_family_linked"] == 1
    assert len(fake.families) == len(fake.relationships) == 1
    assert len(audits) == 1
    assert all("opportunity_signals" not in sql for sql, _ in fake.statements)
    assert all("signal_enrichments SET" not in sql for sql, _ in fake.statements)
    assert all("planning_origin_recovery_attempts" not in sql for sql, _ in fake.statements)


def test_historical_family_backfill_resolves_stored_origin_locally(monkeypatch) -> None:
    row = followup_row()
    origin_id = uuid4()
    origin = (
        origin_id,
        "plota:origin-1",
        {
            "council": "Croydon",
            "planning_status": "approved",
            "provider_record": {"reference": "24/03385/FUL"},
        },
        {"planning_subtype": "NEW_HOME_CHANGE_OF_USE"},
    )
    fake = FamilyBackfillConnection([row], [origin])

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr("app.repository.record_admin_audit", lambda *args, **kwargs: None)
    result = backfill_historical_planning_family_metadata(
        Settings(), actor="admin", preview=False
    )
    assert result["exact_stored_origins_available"] == 1
    assert result["stored_origins_resolved_locally"] == 1
    assert result["external_provider_calls"] == 0
    assert any(origin_id in relationship for relationship in fake.relationships)


def test_historical_family_backfill_handles_multiple_references_and_skips_missing_authority(
    monkeypatch,
) -> None:
    multiple = followup_row(references=["24/03385/FUL", "25/00999/FUL"])
    no_authority = followup_row(authority="")
    fake = FamilyBackfillConnection([multiple, no_authority], [])

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = backfill_historical_planning_family_metadata(
        Settings(), actor="admin", preview=True
    )
    assert result["multiple_reference_signals"] == 1
    assert result["references_relationships_created"] == 2
    assert result["missing_authority_or_reference"] == 1
    assert result["skipped"] == 1
