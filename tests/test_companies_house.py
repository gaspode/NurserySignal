from __future__ import annotations

import io
import json
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest
from app.companies_house import (
    CompaniesHouseError,
    CompaniesHouseProvider,
    OrganisationCandidate,
)
from app.config import Settings
from app.organisation_enrichment import organisation_evidence_document
from app.repository import list_organisation_enrichment_candidates


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_exact_legal_name_resolution_uses_basic_auth_without_exposing_key() -> None:
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if "/search/companies" in request.full_url:
            return Response(
                json.dumps(
                    {
                        "items": [
                            {
                                "title": "EXAMPLE CARE LIMITED",
                                "company_number": "12345678",
                                "company_status": "active",
                                "address": {"locality": "Durham"},
                            }
                        ]
                    }
                ).encode()
            )
        return Response(
            json.dumps(
                {
                    "company_name": "EXAMPLE CARE LIMITED",
                    "company_number": "12345678",
                    "company_status": "active",
                    "date_of_creation": "2025-01-02",
                    "registered_office_address": {"locality": "Durham"},
                    "sic_codes": ["87900"],
                }
            ).encode()
        )

    provider = CompaniesHouseProvider(" secret-key\n", opener=opener)
    result = provider.resolve(OrganisationCandidate("op-1", "Example Care Limited"))
    assert result.status == "MATCHED"
    assert result.outcome == "STRONG"
    assert result.company["company_number"] == "12345678"
    assert len(requests) == 2
    assert requests[0].get_method() == "GET"
    assert requests[0].get_header("User-agent") == "SignalHub/1.0"
    assert requests[0].get_header("Authorization").startswith("Basic ")
    assert "secret-key" not in requests[0].full_url


def test_ambiguous_name_does_not_auto_resolve() -> None:
    def opener(request, timeout):
        return Response(
            json.dumps(
                {
                    "items": [
                        {"title": "ACME CARE LIMITED", "company_number": "11111111"},
                        {"title": "ACME CARE GROUP LIMITED", "company_number": "22222222"},
                    ]
                }
            ).encode()
        )

    result = CompaniesHouseProvider("key", opener=opener).resolve(
        OrganisationCandidate("op-1", "Acme Care")
    )
    assert result.status == "AMBIGUOUS"
    assert result.outcome == "UNCERTAIN"
    assert result.company is None


def test_http_error_is_bounded_and_redacts_api_key() -> None:
    key = "private-key"

    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(f"authorization={key}".encode()),
        )

    with pytest.raises(CompaniesHouseError) as error:
        CompaniesHouseProvider(key, opener=opener).search("Example")
    assert key not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_signal_only_operator_is_materialised_as_enrichment_candidate(monkeypatch) -> None:
    class Result:
        def __init__(self, *, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        committed = False

        def execute(self, sql, params=()):
            if "WITH activity AS" in sql:
                assert params[:3] == (
                    "CHILDRENS_HOME",
                    "CHILDRENS_HOME",
                    "CHILDRENS_HOME",
                )
                return Result(rows=[("Acme Care Limited", None, "Coventry", 3)])
            if "SELECT name, companies_house_number" in sql:
                return Result(row=("Acme Care Limited", None, None))
            return Result()

        def commit(self):
            self.committed = True

    conn = Connection()

    @contextmanager
    def fake_connection(_settings):
        yield conn

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr("app.repository._resolve_operator_id", lambda *_args: "operator-1")

    candidates = list_organisation_enrichment_candidates(
        Settings(database_url="postgresql://unused"),
        limit=10,
        vertical="CHILDRENS_HOME",
    )

    assert candidates == [
        {
            "operator_id": "operator-1",
            "name": "Acme Care Limited",
            "company_number": None,
            "locality": "Coventry",
        }
    ]
    assert conn.committed is True


def test_companies_house_evidence_identity_ignores_retrieval_time() -> None:
    first = organisation_evidence_document(
        query_name="Acme Care Limited",
        status="MATCHED",
        outcome="STRONG",
        confidence=0.95,
        reason="unique normalized legal-name match",
        company={"company_number": "12345678"},
        candidates=[],
    )
    second = organisation_evidence_document(
        query_name="Acme Care Limited",
        status="MATCHED",
        outcome="STRONG",
        confidence=0.95,
        reason="unique normalized legal-name match",
        company={"company_number": "12345678"},
        candidates=[],
    )

    assert first == second
    assert "retrieved_at" not in first
