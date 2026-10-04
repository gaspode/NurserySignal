"""Non-VPC, exact-URL-only fetcher for approved official Planning pages.

This Lambda deliberately has no database, secrets, S3 or business-logic imports.
It is not a proxy: a request must be an exact Idox application details URL hosted
by a reviewed local-authority hostname supplied in its non-secret environment.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BODY_BYTES = 180_000
MAX_REDIRECTS = 2
USER_AGENT = "SignalHub/1.0 official-planning-party-preview (contact: support@signalhub.co.uk)"


def allowed_hosts() -> frozenset[str]:
    return frozenset(
        value.strip().lower()
        for value in os.getenv("OFFICIAL_PLANNING_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    )


def _public_dns(host: str) -> bool:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not addresses:
        return False
    for _family, _kind, _proto, _canonname, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            return False
    return True


def validate_official_url(url: str, *, hosts: frozenset[str] | None = None) -> str | None:
    """Validate a supported public Idox details URL before opening a socket."""
    parsed = urlparse(str(url or ""))
    approved = allowed_hosts() if hosts is None else hosts
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host not in approved:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if not parsed.path.lower().endswith("/online-applications/applicationdetails.do"):
        return None
    query = parse_qs(parsed.query, keep_blank_values=False)
    if not query.get("keyVal"):
        return None
    if not _public_dns(host):
        return None
    return parsed.geturl()


@dataclass
class _SameHostRedirects(HTTPRedirectHandler):
    host: str
    redirects: int = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        self.redirects += 1
        parsed = urlparse(newurl)
        if self.redirects > MAX_REDIRECTS or (parsed.hostname or "").lower() != self.host:
            return None
        if not validate_official_url(newurl, hosts=frozenset({self.host})):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, opener_factory=build_opener) -> dict[str, Any]:
    """Fetch one validated page and return only bounded, safe response data."""
    requested_url = validate_official_url(url)
    if not requested_url:
        return _result("HOST_NOT_ALLOWED", url)
    host = urlparse(requested_url).hostname or ""
    redirects = _SameHostRedirects(host.lower())
    opener = opener_factory(redirects)
    request = Request(requested_url, headers={"Accept": "text/html", "User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=5) as response:
            final_url = response.geturl()
            if not validate_official_url(final_url, hosts=frozenset({host.lower()})):
                return _result("REDIRECT_BLOCKED", requested_url, final_url=final_url)
            content_type = str(response.headers.get_content_type() or "").lower()
            if content_type not in {"text/html", "text/plain"}:
                return _result(
                    "UNSUPPORTED_CONTENT_TYPE",
                    requested_url,
                    final_url=final_url,
                    content_type=content_type,
                )
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                return _result("RESPONSE_TOO_LARGE", requested_url, final_url=final_url)
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            return _result("REDIRECT_BLOCKED", requested_url, http_status=exc.code)
        category = (
            "RATE_LIMITED" if exc.code == 429 else "HTTP_4XX" if exc.code < 500 else "HTTP_5XX"
        )
        return _result(category, requested_url, http_status=exc.code)
    except TimeoutError:
        return _result("TIMEOUT", requested_url)
    except URLError as exc:
        return _result("SOURCE_UNAVAILABLE", requested_url, detail=type(exc.reason).__name__)
    except OSError as exc:
        return _result("SOURCE_UNAVAILABLE", requested_url, detail=type(exc).__name__)
    return {
        "status": "OK",
        "requested_url": requested_url,
        "final_url": final_url,
        "http_status": 200,
        "content_type": content_type,
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "body_base64": base64.b64encode(body).decode("ascii"),
        "error_category": None,
        "detail": None,
    }


def _result(
    category: str,
    requested_url: str,
    *,
    final_url: str | None = None,
    http_status: int | None = None,
    content_type: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "requested_url": requested_url,
        "final_url": final_url,
        "http_status": http_status,
        "content_type": content_type,
        "content_sha256": None,
        "body_base64": None,
        "error_category": category,
        "detail": detail,
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point; accepts exactly one URL and performs no mutations."""
    if set(event) != {"url"} or not isinstance(event.get("url"), str):
        return _result("HOST_NOT_ALLOWED", "", detail="one exact URL is required")
    return fetch(event["url"])
