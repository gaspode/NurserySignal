from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest
from app.companies_house import (
    CompaniesHouseError,
    CompaniesHouseProvider,
    OrganisationCandidate,
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
