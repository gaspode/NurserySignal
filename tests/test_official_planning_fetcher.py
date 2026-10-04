from __future__ import annotations

import base64
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

from app.config import Settings
from app.official_planning_fetch import fetch_idox_party_via_fetcher
from app.official_planning_fetcher import MAX_BODY_BYTES, fetch, validate_official_url

URL = "https://pa.brent.gov.uk/online-applications/applicationDetails.do?keyVal=ABC"
HOSTS = frozenset({"pa.brent.gov.uk"})


class Headers:
    def __init__(self, content_type: str = "text/html"):
        self.content_type = content_type

    def get_content_type(self) -> str:
        return self.content_type


class Response:
    def __init__(self, body: bytes, *, url: str = URL, content_type: str = "text/html"):
        self.body = body
        self.url = url
        self.headers = Headers(content_type)

    def read(self, _limit: int) -> bytes:
        return self.body

    def geturl(self) -> str:
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Opener:
    def __init__(self, response: Response | Exception):
        self.response = response

    def open(self, _request, *, timeout: int):
        assert timeout == 5
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def opener_factory(response: Response | Exception):
    def factory(*_handlers):
        return Opener(response)

    return factory


def test_url_validation_allows_only_exact_official_idox_hosts(monkeypatch) -> None:
    monkeypatch.setattr("app.official_planning_fetcher._public_dns", lambda _host: True)
    assert validate_official_url(URL, hosts=HOSTS) == URL
    for unsafe_url in (
        "http://pa.brent.gov.uk/online-applications/applicationDetails.do?keyVal=A",
        "https://localhost/online-applications/applicationDetails.do?keyVal=A",
        "https://127.0.0.1/online-applications/applicationDetails.do?keyVal=A",
        "https://evil.example/online-applications/applicationDetails.do?keyVal=A",
    ):
        assert validate_official_url(unsafe_url, hosts=HOSTS) is None
    assert validate_official_url("https://pa.brent.gov.uk/anything?keyVal=A", hosts=HOSTS) is None


def test_fetch_handles_bounded_html_and_safe_failures(monkeypatch) -> None:
    monkeypatch.setattr("app.official_planning_fetcher.allowed_hosts", lambda: HOSTS)
    monkeypatch.setattr("app.official_planning_fetcher._public_dns", lambda _host: True)
    success = fetch(URL, opener_factory=opener_factory(Response(b"<html>Applicant Name</html>")))
    assert success["status"] == "OK"
    assert base64.b64decode(success["body_base64"]) == b"<html>Applicant Name</html>"
    too_large = fetch(URL, opener_factory=opener_factory(Response(b"x" * (MAX_BODY_BYTES + 1))))
    assert too_large["error_category"] == "RESPONSE_TOO_LARGE"
    bad_type = fetch(
        URL,
        opener_factory=opener_factory(Response(b"pdf", content_type="application/pdf")),
    )
    assert bad_type["error_category"] == "UNSUPPORTED_CONTENT_TYPE"


def test_fetch_classifies_http_timeout_and_redirect_failures(monkeypatch) -> None:
    monkeypatch.setattr("app.official_planning_fetcher.allowed_hosts", lambda: HOSTS)
    monkeypatch.setattr("app.official_planning_fetcher._public_dns", lambda _host: True)
    assert fetch(
        URL,
        opener_factory=opener_factory(HTTPError(URL, 429, "rate", {}, BytesIO())),
    )["error_category"] == "RATE_LIMITED"
    assert fetch(
        URL,
        opener_factory=opener_factory(HTTPError(URL, 404, "missing", {}, BytesIO())),
    )["error_category"] == "HTTP_4XX"
    assert fetch(
        URL,
        opener_factory=opener_factory(HTTPError(URL, 500, "server", {}, BytesIO())),
    )["error_category"] == "HTTP_5XX"
    assert fetch(URL, opener_factory=opener_factory(TimeoutError()))["error_category"] == "TIMEOUT"
    assert fetch(
        URL,
        opener_factory=opener_factory(Response(b"x", url="https://evil.example/redirect?keyVal=A")),
    )["error_category"] == "REDIRECT_BLOCKED"
    unavailable = fetch(URL, opener_factory=opener_factory(URLError("unavailable")))
    assert unavailable["error_category"] == "SOURCE_UNAVAILABLE"


def test_backend_invocation_contract_parses_only_fetcher_html() -> None:
    class Payload:
        def read(self):
            return json.dumps(
                {
                    "status": "OK",
                    "final_url": URL,
                    "body_base64": base64.b64encode(
                        b"<th>Applicant Name</th><td>Example Care Ltd</td>"
                    ).decode(),
                }
            ).encode()

    class Client:
        def invoke(self, **kwargs):
            assert kwargs["FunctionName"] == "fetcher"
            assert json.loads(kwargs["Payload"].decode()) == {"url": URL}
            return {"Payload": Payload()}

    result = fetch_idox_party_via_fetcher(
        Settings(official_planning_fetcher_function_name="fetcher"), URL, lambda_client=Client()
    )
    assert result.outcome == "APPLICANT_COMPANY_FOUND"


def test_backend_fetch_failure_is_non_mutating_source_result() -> None:
    class Payload:
        def read(self):
            return b'{"status":"ERROR","error_category":"HOST_NOT_ALLOWED"}'

    class Client:
        def invoke(self, **_kwargs):
            return {"Payload": Payload()}

    result = fetch_idox_party_via_fetcher(
        Settings(official_planning_fetcher_function_name="fetcher"), URL, lambda_client=Client()
    )
    assert result.outcome == "HOST_NOT_ALLOWED"
