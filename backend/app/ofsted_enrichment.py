from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import Settings
from app.repository import store_ofsted_urn_enrichment
from app.storage import put_raw_evidence

OFSTED_PROVIDER_URL = "https://reports.ofsted.gov.uk/provider/2/{urn}"
PARSER_VERSION = "ofsted-urn-v1"
USER_AGENT = "SignalHub/1.0"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 12 * 1024 * 1024


class OfstedUrnEnrichmentError(RuntimeError):
    pass


def _date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in ("%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    return None


def _clean_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _bounded_get(
    url: str,
    *,
    accept: str,
    max_bytes: int,
    opener: Any = urlopen,
    timeout: float = 20,
) -> bytes:
    request = Request(
        url,
        method="GET",
        headers={"Accept": accept, "User-Agent": USER_AGENT},
    )
    with opener(request, timeout=timeout) as response:
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise OfstedUrnEnrichmentError("Ofsted response exceeds bounded size")
    return content


def parse_provider_page(page: bytes, urn: str) -> dict[str, Any]:
    text = page.decode("utf-8", errors="replace")
    canonical = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', text, re.I)
    provider_page_url = canonical.group(1) if canonical else OFSTED_PROVIDER_URL.format(urn=urn)
    if urlparse(provider_page_url).netloc != "reports.ofsted.gov.uk":
        provider_page_url = OFSTED_PROVIDER_URL.format(urn=urn)

    details: dict[str, str] = {}
    for label, value in re.findall(
        r"<li[^>]*>\s*<span>\s*([^<:]+):?\s*</span>\s*<span>(.*?)</span>\s*</li>",
        text,
        re.I | re.S,
    ):
        details[_clean_html(label).lower()] = _clean_html(value)

    registration_date = None
    registered = re.search(
        r'<li[^>]*class="[^"]*timeline__day--registered[^"]*"[^>]*>(.*?)</li>',
        text,
        re.I | re.S,
    )
    if registered:
        value = re.search(r"<time[^>]*>(.*?)</time>", registered.group(1), re.I | re.S)
        registration_date = _date(_clean_html(value.group(1))) if value else None

    reports: list[dict[str, Any]] = []
    for item in re.findall(
        r'<li[^>]*class="[^"]*timeline__day[^"]*"[^>]*>(.*?)</li>',
        text,
        re.I | re.S,
    ):
        link = re.search(
            r'<a[^>]*class="[^"]*publication-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            item,
            re.I | re.S,
        )
        if not link:
            continue
        report_url = html.unescape(link.group(1)).strip()
        if urlparse(report_url).netloc != "files.ofsted.gov.uk":
            continue
        inspection_match = re.search(
            r'<p[^>]*class="[^"]*timeline__date[^"]*"[^>]*>\s*<time[^>]*>(.*?)</time>',
            item,
            re.I | re.S,
        )
        published_matches = re.findall(
            r"Published\s*<time[^>]*>(.*?)</time>", item, re.I | re.S
        )
        reports.append(
            {
                "report_type": re.sub(
                    r"\s*(?:Full inspection|Monitoring visit), PDF.*$",
                    "",
                    _clean_html(link.group(2)),
                    flags=re.I,
                ).strip()
                or "Inspection report",
                "inspection_date": (
                    _date(_clean_html(inspection_match.group(1)))
                    if inspection_match
                    else None
                ),
                "publication_date": (
                    _date(_clean_html(published_matches[-1])) if published_matches else None
                ),
                "report_url": report_url,
            }
        )
    reports.sort(
        key=lambda value: value.get("publication_date")
        or value.get("inspection_date")
        or date.min,
        reverse=True,
    )
    latest = reports[0] if reports else None
    return {
        "urn": urn,
        "provider_page_url": provider_page_url,
        "provision_type": details.get("type"),
        "local_authority": details.get("local authority"),
        "registration_date": registration_date.isoformat() if registration_date else None,
        "latest_report_type": latest.get("report_type") if latest else None,
        "latest_report_date": (
            latest["inspection_date"].isoformat()
            if latest and latest.get("inspection_date")
            else None
        ),
        "latest_report_publication_date": (
            latest["publication_date"].isoformat()
            if latest and latest.get("publication_date")
            else None
        ),
        "latest_report_url": latest.get("report_url") if latest else None,
        "report_count": len(reports),
    }


def _decode_pdf_literal(value: bytes) -> str:
    result = bytearray()
    index = 0
    escaped = {
        ord("n"): ord("\n"),
        ord("r"): ord("\r"),
        ord("t"): ord("\t"),
        ord("b"): 8,
        ord("f"): 12,
    }
    while index < len(value):
        current = value[index]
        if current != 92:
            result.append(current)
            index += 1
            continue
        index += 1
        if index >= len(value):
            break
        current = value[index]
        if current in escaped:
            result.append(escaped[current])
            index += 1
        elif current in b"()\\":
            result.append(current)
            index += 1
        elif 48 <= current <= 55:
            digits = bytes([current])
            index += 1
            while index < len(value) and len(digits) < 3 and 48 <= value[index] <= 55:
                digits += bytes([value[index]])
                index += 1
            result.append(int(digits, 8))
        elif current in b"\r\n":
            if current == 13 and index + 1 < len(value) and value[index + 1] == 10:
                index += 1
            index += 1
        else:
            result.append(current)
            index += 1
    return result.decode("utf-8", errors="replace")


def parse_report_pdf(report: bytes) -> dict[str, Any]:
    actual_text = "".join(
        _decode_pdf_literal(item)
        for item in re.findall(rb"/ActualText\(((?:\\.|[^\\)])*)\)", report)
    )
    normalized = " ".join(actual_text.split())
    provider_matches = re.findall(
        r"Registered provider:\s*(.+?)(?=Registered provider address:|"
        r"Responsible individual:|Registered manager:|Inspectors)",
        normalized,
        re.I,
    )
    address_matches = re.findall(
        r"Registered provider address:\s*(.+?)(?=Responsible individual:|"
        r"Registered manager:|Inspectors)",
        normalized,
        re.I,
    )
    provider_name = provider_matches[-1].strip() if provider_matches else None
    provider_address = address_matches[-1].strip() if address_matches else None
    postcode_match = re.search(
        r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", provider_address or "", re.I
    )
    provider_postcode = (
        re.sub(r"\s+", " ", postcode_match.group(1).upper()) if postcode_match else None
    )
    address_without_postcode = (
        (provider_address or "").replace(postcode_match.group(0), "").strip(" ,")
        if postcode_match
        else provider_address
    )
    parts = [part.strip() for part in (address_without_postcode or "").split(",") if part.strip()]
    provider_region = parts[-1] if len(parts) >= 2 else None
    provider_locality = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else None)
    return {
        "registered_provider_name": provider_name,
        "provider_registered_address": provider_address,
        "provider_registered_locality": provider_locality,
        "provider_registered_region": provider_region,
        "provider_registered_postcode": provider_postcode,
        "report_content_sha256": hashlib.sha256(report).hexdigest(),
    }


def enrich_ofsted_urn(
    urn: str,
    *,
    opener: Any = urlopen,
    retrieved_at: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"(?:SC)?\d{6,8}", urn, re.I):
        raise ValueError("invalid Ofsted URN")
    retrieved_at = retrieved_at or datetime.now(UTC)
    page_url = OFSTED_PROVIDER_URL.format(urn=urn)
    page = _bounded_get(
        page_url,
        accept="text/html,application/xhtml+xml",
        max_bytes=MAX_PAGE_BYTES,
        opener=opener,
    )
    result = parse_provider_page(page, urn)
    result.update(
        {
            "status": "SUCCEEDED" if result.get("latest_report_url") else "NO_REPORT",
            "parser_version": PARSER_VERSION,
            "retrieved_at": retrieved_at.isoformat(),
            "failure_category": None,
        }
    )
    report_url = result.get("latest_report_url")
    if report_url:
        try:
            report = _bounded_get(
                str(report_url),
                accept="application/pdf",
                max_bytes=MAX_REPORT_BYTES,
                opener=opener,
            )
            result.update(parse_report_pdf(report))
        except Exception as exc:
            result["status"] = "PARTIAL"
            result["failure_category"] = type(exc).__name__[:80]
    return result


def stable_enrichment_document(value: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "urn",
        "status",
        "provider_page_url",
        "provision_type",
        "registration_date",
        "local_authority",
        "registered_provider_name",
        "provider_registered_address",
        "provider_registered_locality",
        "provider_registered_region",
        "provider_registered_postcode",
        "latest_report_type",
        "latest_report_date",
        "latest_report_publication_date",
        "latest_report_url",
        "report_count",
        "report_content_sha256",
        "parser_version",
        "failure_category",
    )
    return {key: value.get(key) for key in allowed}


def persist_ofsted_urn_enrichment(
    settings: Settings, signal_id: str, value: dict[str, Any]
) -> bool:
    if not settings.evidence_bucket:
        raise RuntimeError("EVIDENCE_BUCKET is not configured")
    stable = stable_enrichment_document(value)
    urn = str(stable.get("urn") or "").strip()
    if not urn:
        raise ValueError("Ofsted enrichment has no URN")
    payload = json.dumps(stable, separators=(",", ":"), sort_keys=True).encode()
    content_hash = hashlib.sha256(payload).hexdigest()
    safe_urn = re.sub(r"[^A-Za-z0-9-]+", "-", urn)
    key = f"regulatory/ofsted/{safe_urn}/{content_hash}.json"
    put_raw_evidence(settings, settings.evidence_bucket, key, payload)
    return store_ofsted_urn_enrichment(
        settings,
        signal_id=signal_id,
        value=stable,
        evidence_bucket=settings.evidence_bucket,
        evidence_key=key,
        content_sha256=content_hash,
        retrieved_at=str(value.get("retrieved_at") or datetime.now(UTC).isoformat()),
    )
