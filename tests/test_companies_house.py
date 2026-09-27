from __future__ import annotations

import io
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest
from app.companies_house import (
    CompaniesHouseError,
    CompaniesHouseProvider,
    OrganisationCandidate,
)
from app.config import Settings
from app.organisation_enrichment import (
    organisation_evidence_document,
    process_organisation_enrichment,
)
from app.repository import (
    list_organisation_enrichment_candidates,
    list_organisation_match_reviews,
    resolve_organisation_match_review,
)


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
        if "/company/" in request.full_url:
            company_number = request.full_url.rsplit("/", 1)[-1]
            return Response(
                json.dumps(
                    {
                        "company_name": (
                            "ACME CARE LIMITED"
                            if company_number == "11111111"
                            else "ACME CARE GROUP LIMITED"
                        ),
                        "company_number": company_number,
                        "company_status": "active",
                        "date_of_creation": "2020-03-04",
                        "company_type": "ltd",
                        "registered_office_address": {
                            "locality": "Coventry",
                            "postal_code": "CV1 2AB",
                        },
                        "sic_codes": ["87900"],
                    }
                ).encode()
            )
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
    assert result.candidates[0]["company_status"] == "active"
    assert result.candidates[0]["date_of_creation"] == "2020-03-04"
    assert result.candidates[0]["sic_descriptions"] == [
        {
            "code": "87900",
            "description": "Other residential care activities not elsewhere classified",
        }
    ]
    assert result.candidates[0]["match_outcome"] == "PROBABLE"
    assert "Name match only; no location corroboration" in result.candidates[0][
        "match_cautions"
    ]


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
            if "WITH activity" in sql:
                assert params[:3] == (
                    "CHILDRENS_HOME",
                    "CHILDRENS_HOME",
                    "CHILDRENS_HOME",
                )
                return Result(
                    rows=[
                        (
                            "Acme Care Limited",
                            None,
                            "Coventry",
                            "CV1 2AB",
                            "1 Example Road",
                            "https://acme.example",
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
            "postcode": "CV1 2AB",
            "address": "1 Example Road",
            "website": "https://acme.example",
            "provider_registered_name": None,
            "provider_registered_locality": None,
            "provider_registered_postcode": None,
            "provider_registered_address": None,
        }
    ]
    assert conn.committed is True


def test_new_ofsted_provider_evidence_bypasses_stale_company_cache(monkeypatch) -> None:
    provider_evidence_at = datetime.now(UTC)

    class Result:
        def __init__(self, *, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, params=()):
            if "WITH activity" in sql:
                return Result(
                    rows=[
                        (
                            "Oaktree Childcare Ltd",
                            None,
                            "Lancashire",
                            None,
                            None,
                            None,
                            "Oaktree Childcare Limited",
                            "Blackpool",
                            "FY4 2FF",
                            "Provider office, Blackpool, FY4 2FF",
                            provider_evidence_at,
                            2,
                        )
                    ]
                )
            if "SELECT name, companies_house_number" in sql:
                return Result(
                    row=(
                        "Oaktree Childcare Ltd",
                        None,
                        provider_evidence_at - timedelta(minutes=1),
                    )
                )
            return Result()

        def commit(self):
            return None

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr("app.repository._resolve_operator_id", lambda *_args: "operator-1")
    result = list_organisation_enrichment_candidates(Settings(), limit=1)
    assert result[0]["provider_registered_name"] == "Oaktree Childcare Limited"
    assert result[0]["provider_registered_postcode"] == "FY4 2FF"


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


def test_rejected_candidate_set_is_not_immediately_recreated(monkeypatch) -> None:
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        inserted_review = False

        def execute(self, sql, params=()):
            if "SELECT id, name, companies_house_number" in sql:
                return Result(("operator-1", "Other Care", None))
            if "status = 'REJECTED'" in sql:
                return Result((1,))
            if "INSERT INTO organisation_match_reviews" in sql:
                self.inserted_review = True
            return Result()

        def commit(self):
            return None

    conn = Connection()

    @contextmanager
    def fake_connection(_settings):
        yield conn

    monkeypatch.setattr("app.organisation_enrichment.connection", fake_connection)
    monkeypatch.setattr("app.organisation_enrichment.put_raw_evidence", lambda *args: None)
    result = process_organisation_enrichment(
        Settings(evidence_bucket="evidence"),
        {
            "provider": "COMPANIES_HOUSE",
            "operator_id": "operator-1",
            "query_name": "Other Care",
            "status": "AMBIGUOUS",
            "outcome": "UNCERTAIN",
            "confidence": 0.8,
            "reason": "multiple candidates",
            "retrieved_at": "2026-09-27T12:00:00Z",
            "candidates": [
                {"company_name": "OTHER CARE LIMITED", "company_number": "87654321"}
            ],
        },
    )
    assert result["status"] == "AMBIGUOUS"
    assert conn.inserted_review is False


def test_strong_enrichment_supersedes_an_existing_pending_review(monkeypatch) -> None:
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        statements = []

        def execute(self, sql, params=()):
            self.statements.append((sql, params))
            if "SELECT id, name, companies_house_number" in sql:
                return Result(("operator-1", "Oaktree Childcare Ltd", None))
            return Result()

        def commit(self):
            return None

    conn = Connection()

    @contextmanager
    def fake_connection(_settings):
        yield conn

    monkeypatch.setattr("app.organisation_enrichment.connection", fake_connection)
    monkeypatch.setattr("app.organisation_enrichment.put_raw_evidence", lambda *args: None)
    result = process_organisation_enrichment(
        Settings(evidence_bucket="evidence"),
        {
            "provider": "COMPANIES_HOUSE",
            "operator_id": "operator-1",
            "query_name": "Oaktree Childcare Limited",
            "status": "MATCHED",
            "outcome": "STRONG",
            "confidence": 0.99,
            "reason": "Ofsted provider identity corroborated",
            "retrieved_at": "2026-09-27T12:00:00Z",
            "company": {
                "company_number": "10445560",
                "company_name": "OAKTREE CHILDCARE LIMITED",
                "company_status": "active",
                "registered_office_address": {"postal_code": "FY4 2FF"},
            },
            "candidates": [],
        },
    )
    assert result["status"] == "MATCHED"
    assert any(
        "UPDATE organisation_match_reviews" in sql and "SUPERSEDED" in sql
        for sql, _params in conn.statements
    )


def test_organisation_review_includes_source_context(monkeypatch) -> None:
    class Result:
        def __init__(self, *, rows=None, row=None):
            self.rows = rows or []
            self.row = row

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.row

    class Connection:
        def execute(self, sql, params=()):
            if "FROM organisation_match_reviews r" in sql:
                return Result(
                    rows=[
                        (
                            "review-1",
                            "operator-1",
                            "Other Care",
                            "COMPANIES_HOUSE",
                            "Other Care Ltd",
                            [{"company_number": "87654321"}],
                            "multiple candidates",
                            "2026-09-27T12:00:00Z",
                        )
                    ]
                )
            if "SELECT name, legal_name, website_url" in sql:
                return Result(row=("Other Care", None, "https://other.example"))
            if "FROM organisation_aliases" in sql:
                return Result(rows=[("Other Care Ltd", "SOURCE")])
            if "FROM opportunities" in sql:
                return Result(
                    rows=[
                        (
                            "opportunity-1",
                            "New children's home — Coventry",
                            "CHILDRENS_HOME",
                            "Coventry",
                            "CV1 2AB",
                            "1 Example Road",
                        )
                    ]
                )
            if "FROM raw_signals rs" in sql:
                return Result(
                    rows=[
                        (
                            "signal-1",
                            "Change of use to children's home",
                            "CHILDRENS_HOME",
                            "planning",
                            "https://planning.example/1",
                            "Coventry CV1 2AB",
                            "Other Care Ltd",
                            {"postcode": "CV1 2AB", "council": "Coventry"},
                            "2026-09-20T12:00:00Z",
                        )
                    ]
                )
            if "FROM ofsted_urn_enrichments" in sql:
                return Result(
                    rows=[
                        (
                            "2766766",
                            "Other Care Limited",
                            "Children's Home",
                            "2024-10-04",
                            "Lancashire",
                            "1 Provider Office, Blackpool, FY4 2FF",
                            "Blackpool",
                            "Lancashire",
                            "FY4 2FF",
                            "2026-07-28",
                            "2026-09-08",
                            "https://files.ofsted.gov.uk/v1/file/50311430",
                            "https://reports.ofsted.gov.uk/provider/2/2766766",
                            "2026-09-27T12:00:00Z",
                        )
                    ]
                )
            return Result()

    @contextmanager
    def fake_connection(_settings):
        yield Connection()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    reviews = list_organisation_match_reviews(Settings(), limit=25)
    assert reviews[0]["source_context"]["verticals"] == ["CHILDRENS_HOME"]
    assert reviews[0]["source_context"]["source_types"] == ["planning"]
    assert reviews[0]["source_context"]["signals"][0]["postcode"] == "CV1 2AB"
    assert reviews[0]["source_context"]["opportunities"][0]["id"] == "opportunity-1"
    assert reviews[0]["source_context"]["ofsted_evidence"][0]["urn"] == "2766766"
    assert reviews[0]["source_context"]["ofsted_evidence"][0][
        "provider_registered_postcode"
    ] == "FY4 2FF"


def test_ofsted_provider_identity_and_office_can_make_company_match_strong() -> None:
    def opener(request, timeout):
        if "/company/" in request.full_url:
            return Response(
                json.dumps(
                    {
                        "company_name": "OAKTREE CHILDCARE LIMITED",
                        "company_number": "10445560",
                        "company_status": "active",
                        "registered_office_address": {
                            "locality": "Blackpool",
                            "region": "Lancashire",
                            "postal_code": "FY4 2FF",
                        },
                        "sic_codes": ["87900"],
                    }
                ).encode()
            )
        return Response(
            json.dumps(
                {
                    "items": [
                        {
                            "title": "OAKTREE CHILDCARE LIMITED",
                            "company_number": "10445560",
                            "company_status": "active",
                            "address": {
                                "locality": "Blackpool",
                                "region": "Lancashire",
                                "postal_code": "FY4 2FF",
                            },
                        },
                        {
                            "title": "OAKTREE TRAINING AND CHILDCARE SERVICES LIMITED",
                            "company_number": "09385309",
                            "company_status": "active",
                            "address": {
                                "locality": "London",
                                "postal_code": "N1 1AA",
                            },
                        },
                    ]
                }
            ).encode()
        )

    result = CompaniesHouseProvider("key", opener=opener).resolve(
        OrganisationCandidate(
            "operator-1",
            "Oaktree Childcare Ltd",
            provider_registered_name="Oaktree Childcare Limited",
            provider_registered_locality="Blackpool",
            provider_registered_postcode="FY4 2FF",
        )
    )
    assert result.status == "MATCHED"
    assert result.outcome == "STRONG"
    assert result.confidence == 0.99
    assert "Ofsted provider address" in result.reason
    assert result.company["company_number"] == "10445560"


def test_confirmed_organisation_review_preserves_aliases_and_audit(monkeypatch) -> None:
    class Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        statements = []

        def execute(self, sql, params=()):
            self.statements.append((sql, params))
            if "FROM organisation_match_reviews WHERE id" in sql:
                return Result(
                    (
                        "review-1",
                        "operator-1",
                        [
                            {
                                "company_number": "87654321",
                                "company_name": "OTHER CARE LIMITED",
                                "company_status": "active",
                                "date_of_creation": "2020-03-04",
                                "type": "ltd",
                                "registered_office_address": {"locality": "Coventry"},
                                "sic_codes": ["87900"],
                            }
                        ],
                        "PENDING",
                        "Other Care",
                    )
                )
            return Result()

        def commit(self):
            return None

    conn = Connection()

    @contextmanager
    def fake_connection(_settings):
        yield conn

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = resolve_organisation_match_review(
        Settings(),
        "review-1",
        action="confirm",
        actor="admin-1",
        company_number="87654321",
    )
    assert result["status"] == "CONFIRMED"
    assert any("INSERT INTO organisation_aliases" in sql for sql, _ in conn.statements)
    assert any("INSERT INTO admin_audit_events" in sql for sql, _ in conn.statements)
