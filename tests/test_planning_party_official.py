from __future__ import annotations

from io import BytesIO
from urllib.error import HTTPError, URLError

from app.planning_party_official import (
    fetch_idox_party_page,
    idox_details_url,
    official_party_provenance,
)


class Response:
    def __init__(self, body: str):
        self._body = body.encode()

    def read(self, _limit: int) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_idox_adapter_uses_exact_details_url_and_extracts_labelled_applicant() -> None:
    requested = []

    def opener(request, *, timeout):
        requested.append((request.full_url, timeout))
        return Response(
            "<th>Applicant Name:</th><td>Example Care Ltd</td>"
            "<th>Agent Name:</th><td>Jane Planner</td>"
            "<th>Agent Company Name:</th><td>Planning Agent LLP</td>"
        )

    result = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?"
        "keyVal=ABC&activeTab=summary",
        opener=opener,
    )
    assert result.outcome == "APPLICANT_COMPANY_FOUND"
    assert "activeTab=details" in requested[0][0]
    provenance = official_party_provenance(result, application_reference="24/1/FUL")
    assert provenance["applicant"]["role"] == "APPLICANT"
    assert provenance["agent"]["role"] == "AGENT"


def test_idox_adapter_distinguishes_person_agent_only_and_no_data() -> None:
    person = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=A",
        opener=lambda *_args, **_kwargs: Response("<th>Applicant Name</th><td>Jane Smith</td>"),
    )
    agent = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=B",
        opener=lambda *_args, **_kwargs: Response("<th>Agent Name</th><td>Planner LLP</td>"),
    )
    empty = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=C",
        opener=lambda *_args, **_kwargs: Response("<th>Applicant Name</th><td></td>"),
    )
    assert person.outcome == "APPLICANT_PERSON_FOUND"
    assert agent.outcome == "AGENT_ONLY"
    assert empty.outcome == "NO_PARTY_DATA"


def test_idox_adapter_handles_unavailable_rate_limit_and_unsupported_without_search() -> None:
    def rate_limited(*_args, **_kwargs):
        raise HTTPError("https://example", 429, "too many", {}, BytesIO())

    limited = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=A",
        opener=rate_limited,
    )
    unsupported = fetch_idox_party_page("https://planning.example.gov.uk/application/24-1")
    assert limited.outcome == "RATE_LIMITED"
    assert unsupported.outcome == "UNSUPPORTED_AUTHORITY"
    assert idox_details_url("https://planning.example.gov.uk/application/24-1") is None


def test_idox_adapter_retries_transient_timeouts_with_bounded_backoff() -> None:
    attempts = 0
    waits = []

    def opener(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise URLError("temporary")
        return Response("<th>Applicant Name</th><td>Example Care Ltd</td>")

    result = fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=A",
        opener=opener,
        sleeper=waits.append,
    )
    assert result.outcome == "APPLICANT_COMPANY_FOUND"
    assert attempts == 2
    assert waits == [0.2]


def test_idox_adapter_bounds_timeout_supplied_by_preview() -> None:
    timeouts = []

    def opener(_request, *, timeout):
        timeouts.append(timeout)
        return Response("<th>Applicant Name</th><td>Example Care Ltd</td>")

    fetch_idox_party_page(
        "https://planning.example.gov.uk/online-applications/applicationDetails.do?keyVal=A",
        opener=opener,
        timeout=4,
    )
    assert timeouts == [4]
