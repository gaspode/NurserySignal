from __future__ import annotations

from contextlib import contextmanager

import pytest
from app.companies_house import CompanyResolution
from app.companies_house_collector import collect_companies_house
from app.config import Settings
from app.organisation_enrichment import process_organisation_enrichment
from app.organisation_types import is_public_authority_name, public_authority_aliases
from app.repository import (
    _resolve_operator_id,
    list_organisation_enrichment_candidates,
    public_authority_backfill,
    set_organisation_type,
)


@pytest.mark.parametrize(
    "name",
    [
        "Lancashire County Council",
        "Birmingham City Council",
        "Sandwell Metropolitan Borough Council",
        "London Borough of Croydon",
        "Epping Forest District Council",
        "Trafford Council",
        "Manchester City Council",
        "Coventry City Council",
        "Council of the Isles of Scilly",
        "West Midlands Combined Authority",
    ],
)
def test_structural_public_authority_names_are_detected(name) -> None:
    assert is_public_authority_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "Council Tax Solutions Ltd",
        "Care Council Limited",
        "Care Council",
        "Council House Care Ltd",
        "The Council House Nursery",
        "National Council for Voluntary Organisations",
        "Acme Care Group",
        "Council",
        "",
    ],
)
def test_private_or_ambiguous_council_wording_is_not_detected(name) -> None:
    assert is_public_authority_name(name) is False


def test_public_authority_aliases_are_small_and_explainable() -> None:
    assert public_authority_aliases("Lancashire County Council") == (
        "Lancashire CC",
        "Lancashire Council",
        "Lancashire County Council",
    )
    assert public_authority_aliases("London Borough of Croydon") == (
        "Croydon Council",
        "London Borough of Croydon",
    )


def test_companies_house_collector_drops_public_authority_candidates(monkeypatch) -> None:
    class Provider:
        def resolve(self, _candidate):
            raise AssertionError("public authority must not reach Companies House")

    monkeypatch.setattr(
        "app.companies_house_collector.start_run",
        lambda *_args, **_kwargs: ("run-1", "2026-09-30T00:00:00+00:00"),
    )
    monkeypatch.setattr("app.companies_house_collector.finish_run", lambda *_args, **_kwargs: None)
    result = collect_companies_house(
        Settings(),
        {
            "organisation_candidates": [
                {"operator_id": "operator-1", "name": "Birmingham City Council"}
            ]
        },
        provider=Provider(),
    )
    assert result["organisations_attempted"] == 0
    assert result["signals_queued"] == 0


def test_admin_company_override_allows_company_collection(monkeypatch) -> None:
    attempted = []

    class Provider:
        def resolve(self, candidate):
            attempted.append(candidate)
            return CompanyResolution(
                operator_id=candidate.operator_id,
                query_name=candidate.name,
                status="NOT_FOUND",
                outcome="NO_MATCH",
                confidence=0.0,
                reason="No candidate found",
                company=None,
                candidates=(),
            )

    class Queue:
        def send_message(self, **_kwargs):
            return {"MessageId": "message-1"}

    monkeypatch.setattr(
        "app.companies_house_collector.start_run",
        lambda *_args, **_kwargs: ("run-1", "2026-09-30T00:00:00+00:00"),
    )
    monkeypatch.setattr("app.companies_house_collector.finish_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.companies_house_collector.boto3.client", lambda _name: Queue())
    result = collect_companies_house(
        Settings(companies_house_secret_arn="secret", ingestion_queue_url="queue"),
        {
            "organisation_candidates": [
                {
                    "operator_id": "operator-1",
                    "name": "Example Council",
                    "organisation_type": "PRIVATE_COMPANY",
                }
            ]
        },
        provider=Provider(),
    )
    assert result["organisations_attempted"] == 1
    assert len(attempted) == 1


def test_stale_enrichment_message_for_public_authority_creates_no_review(monkeypatch) -> None:
    class Result:
        def fetchone(self):
            return ("Lancashire County Council", "Lancashire County Council", "PUBLIC_AUTHORITY")

    class Connection:
        def execute(self, _sql, _params=()):
            return Result()

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    wrote_evidence = False

    def write_evidence(*_args):
        nonlocal wrote_evidence
        wrote_evidence = True

    monkeypatch.setattr("app.organisation_enrichment.connection", fake_connection)
    monkeypatch.setattr("app.organisation_enrichment.put_raw_evidence", write_evidence)
    result = process_organisation_enrichment(
        Settings(evidence_bucket="evidence"),
        {
            "provider": "COMPANIES_HOUSE",
            "operator_id": "operator-1",
            "query_name": "Lancashire County Council",
        },
    )
    assert result["status"] == "SKIPPED_PUBLIC_AUTHORITY"
    assert result["outcome"] == "NOT_APPLICABLE"
    assert wrote_evidence is False


def test_public_authority_identity_is_created_once_with_aliases() -> None:
    statements = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "SELECT id, name, legal_name" in sql:
                return Result(rows=[])
            if "SELECT operator_id FROM organisation_aliases" in sql:
                return Result()
            if "INSERT INTO operators" in sql:
                assert params[2] is None
                assert params[4] == "PUBLIC_AUTHORITY"
                return Result(row=("operator-1",))
            return Result()

    operator_id = _resolve_operator_id(Connection(), "Lancashire County Council", {})
    assert operator_id == "operator-1"
    aliases = [params[1] for sql, params in statements if "INSERT INTO organisation_aliases" in sql]
    assert aliases == ["Lancashire CC", "Lancashire Council", "Lancashire County Council"]


def test_public_authority_alias_reuses_existing_identity() -> None:
    statements = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "SELECT id, name, legal_name" in sql:
                return Result(rows=[])
            if "SELECT operator_id FROM organisation_aliases" in sql:
                return Result(row=("operator-croydon",))
            if "INSERT INTO operators" in sql:
                raise AssertionError("alias must reuse the existing public authority")
            return Result()

    assert _resolve_operator_id(Connection(), "Croydon Council", {}) == "operator-croydon"


def test_public_authority_identity_does_not_store_source_office_as_site_location() -> None:
    statements = []

    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "SELECT id, name, legal_name" in sql:
                return Result(rows=[])
            if "SELECT operator_id FROM organisation_aliases" in sql:
                return Result()
            if "INSERT INTO operators" in sql:
                return Result(row=("operator-1",))
            return Result()

    _resolve_operator_id(
        Connection(),
        "Birmingham City Council",
        {"address": "Council House, Victoria Square", "postcode": "B1 1BB"},
    )
    insert = next(item for item in statements if "INSERT INTO operators" in item[0])
    assert "address" not in insert[0]
    assert "postcode" not in insert[0]


def test_public_authority_is_materialised_but_not_returned_for_company_search(monkeypatch) -> None:
    class Result:
        def __init__(self, *, row=None, rows=None):
            self.row = row
            self.rows = rows or []

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.rows

    class Connection:
        committed = False

        def execute(self, sql, _params=()):
            if "WITH activity" in sql:
                return Result(
                    rows=[
                        (
                            "Birmingham City Council",
                            None,
                            "Birmingham",
                            "B1 1AA",
                            "Council House",
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            3,
                        )
                    ]
                )
            if "SELECT name, companies_house_number" in sql:
                return Result(
                    row=("Birmingham City Council", None, None, False, "PUBLIC_AUTHORITY")
                )
            return Result()

        def commit(self):
            self.committed = True

    conn = Connection()

    @contextmanager
    def fake_connection(_settings):
        yield conn

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr("app.repository._resolve_operator_id", lambda *_args: "operator-1")
    result = list_organisation_enrichment_candidates(
        Settings(), limit=10, vertical="CHILDRENS_HOME"
    )
    assert result == []
    assert conn.committed is True


def test_manual_public_authority_override_preserves_company_mapping(monkeypatch) -> None:
    statements = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, params=()):
            statements.append((sql, params))
            if "SELECT name, companies_house_number" in sql:
                return Result(("Example City Council", "12345678", "PRIVATE_COMPANY"))
            return Result()

        def commit(self):
            return None

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = set_organisation_type(
        Settings(),
        "operator-1",
        organisation_type="PUBLIC_AUTHORITY",
        actor="admin-1",
    )
    assert result["companies_house_mapping_preserved"] is True
    updates = [sql for sql, _params in statements if "UPDATE operators" in sql]
    assert len(updates) == 1
    assert "companies_house_number" not in updates[0]
    assert not any("UPDATE organisation_match_reviews" in sql for sql, _ in statements)


def test_manual_revert_uses_private_company_override(monkeypatch) -> None:
    updates = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, params=()):
            if "SELECT name, companies_house_number" in sql:
                return Result(("Example Council", None, "PUBLIC_AUTHORITY"))
            if "UPDATE operators SET organisation_type" in sql:
                updates.append(params)
            return Result()

        def commit(self):
            return None

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = set_organisation_type(
        Settings(),
        "operator-1",
        organisation_type="NORMAL_RESOLUTION",
        actor="admin-1",
    )
    assert result["organisation_type"] == "PRIVATE_COMPANY"
    assert updates[0][0] == "PRIVATE_COMPANY"


def test_public_authority_backfill_preview_separates_safe_and_manual_conflicts(
    monkeypatch,
) -> None:
    rows = [
        (
            "operator-1",
            "Lancashire County Council",
            "Lancashire County Council",
            None,
            "UNKNOWN",
            None,
            {},
            4,
            7,
            2,
            False,
        ),
        (
            "operator-2",
            "Birmingham City Council",
            "Birmingham City Council",
            "12345678",
            "PRIVATE_COMPANY",
            "ADMIN",
            {"selection_source": "MANUAL_COMPANY_NUMBER"},
            1,
            2,
            0,
            True,
        ),
        (
            "operator-3",
            "Care Council",
            "Care Council",
            None,
            "UNKNOWN",
            None,
            {},
            0,
            0,
            0,
            False,
        ),
    ]

    class Result:
        def fetchall(self):
            return rows

    class Connection:
        def execute(self, _sql, _params=()):
            return Result()

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = public_authority_backfill(Settings(), apply=False)
    assert result["counts"] == {
        "organisations_inspected": 3,
        "likely_public_authority": 2,
        "no_ch_mapping": 1,
        "pending_ch_review": 1,
        "system_mapped_ch": 0,
        "manually_confirmed_ch": 1,
        "ambiguous_needs_review": 1,
        "already_public_authority": 0,
    }
    assert result["items"][0]["safe_to_correct"] is True
    assert result["items"][1]["mapping_state"] == "MANUAL_CONFLICT"
    assert result["items"][1]["safe_to_correct"] is False
    assert result["manual_conflicts_preserved"] == 1
