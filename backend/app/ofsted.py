from __future__ import annotations

import re
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

OFSTED_REGISTER_PAGE = (
    "https://www.gov.uk/government/publications/"
    "inspection-and-regulation-of-childrens-social-care-providers"
)
DEFAULT_OFSTED_DATA_URL = (
    "https://assets.publishing.service.gov.uk/media/697345cb51bd707cb10ed934/"
    "Inspection_and_regulation_of_childrens_social_care_and_supported_"
    "accommodation_providers_2025.ods"
)

TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
TARGET_SHEET = "Provider_level_at_30_09_25"
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024


@dataclass(frozen=True)
class OfstedQuery:
    max_records: int = 50
    registered_since_days: int = 730
    active_only: bool = True
    urns: tuple[str, ...] = ()
    urn_enrichment_limit: int = 10

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> OfstedQuery:
        raw_urns = event.get("urns") or []
        if not isinstance(raw_urns, list):
            raise ValueError("urns must be a list")
        urns = tuple(
            urn
            for urn in (str(value).strip() for value in raw_urns[:10])
            if urn and re.fullmatch(r"(?:SC)?\d{6,8}", urn, re.I)
        )
        return cls(
            max_records=min(max(int(event.get("max_records", 50)), 1), 200),
            registered_since_days=min(
                max(int(event.get("registered_since_days", 730)), 1), 3650
            ),
            active_only=bool(event.get("active_only", True)),
            urns=urns,
            urn_enrichment_limit=min(
                max(int(event.get("urn_enrichment_limit", 10)), 0), 10
            ),
        )


@dataclass(frozen=True)
class OfstedRecord:
    urn: str
    provider_name: str
    registration_status: str
    registration_date: date | None
    local_authority: str | None
    ofsted_region: str | None
    government_region: str | None
    sector: str | None
    places: int | None
    latest_event_type: str | None
    latest_inspection_date: date | None
    latest_publication_date: date | None
    source_url: str
    raw: dict[str, Any]


def _text(cell: ElementTree.Element) -> str:
    return " ".join(part.strip() for part in cell.itertext() if part.strip())


def _row_values(row: ElementTree.Element) -> tuple[list[str], list[str | None]]:
    values: list[str] = []
    links: list[str | None] = []
    for cell in row.findall(f"{{{TABLE_NS}}}table-cell"):
        repeated = min(
            int(cell.attrib.get(f"{{{TABLE_NS}}}number-columns-repeated", "1")), 200
        )
        value = _text(cell)
        link = next(
            (
                item.attrib.get(f"{{{XLINK_NS}}}href")
                for item in cell.iter()
                if item.tag == f"{{{TEXT_NS}}}a"
                and item.attrib.get(f"{{{XLINK_NS}}}href")
            ),
            None,
        )
        values.extend([value] * repeated)
        links.extend([link] * repeated)
    return values, links


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    return None


def _integer(value: str | None) -> int | None:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None


def download_register(url: str = DEFAULT_OFSTED_DATA_URL) -> Path:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/vnd.oasis.opendocument.spreadsheet",
            "User-Agent": "SignalHub/1.0",
        },
    )
    target = tempfile.NamedTemporaryFile(prefix="signalhub-ofsted-", suffix=".ods", delete=False)
    path = Path(target.name)
    try:
        with target, urllib.request.urlopen(request, timeout=30) as response:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Ofsted register exceeds the bounded download limit")
                target.write(chunk)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def records_from_ods(
    path: Path, query: OfstedQuery, *, today: date | None = None
) -> Iterator[OfstedRecord]:
    today = today or datetime.now(UTC).date()
    earliest = today - timedelta(days=query.registered_since_days)
    requested_urns = {value.upper() for value in query.urns}
    headers: list[str] | None = None
    current_sheet: str | None = None
    yielded = 0
    with zipfile.ZipFile(path) as archive, archive.open("content.xml") as content:
        for event, element in ElementTree.iterparse(content, events=("start", "end")):
            if event == "start" and element.tag == f"{{{TABLE_NS}}}table":
                current_sheet = element.attrib.get(f"{{{TABLE_NS}}}name")
            elif event == "end" and element.tag == f"{{{TABLE_NS}}}table-row":
                if current_sheet != TARGET_SHEET:
                    element.clear()
                    continue
                values, links = _row_values(element)
                element.clear()
                if not values:
                    continue
                if headers is None:
                    if "URN" in values and "Provision type" in values:
                        headers = values
                    continue
                row = {
                    header: values[index] if index < len(values) else ""
                    for index, header in enumerate(headers)
                    if header
                }
                if str(row.get("Provision type") or "").strip().lower() != "children's home":
                    continue
                status = str(row.get("Registration status") or "").strip()
                if query.active_only and status.lower() not in {"active", "registered"}:
                    continue
                registration_date = _parse_date(row.get("Registration date"))
                if not requested_urns and registration_date and registration_date < earliest:
                    continue
                urn = str(row.get("URN") or "").strip()
                provider = str(row.get("Organisation which owns the provider") or "").strip()
                if not urn or not provider:
                    continue
                if requested_urns and urn.upper() not in requested_urns:
                    continue
                source_url = next((link for link in links if link), None) or OFSTED_REGISTER_PAGE
                yield OfstedRecord(
                    urn=urn,
                    provider_name=provider,
                    registration_status=status,
                    registration_date=registration_date,
                    local_authority=str(row.get("Local authority") or "").strip() or None,
                    ofsted_region=str(row.get("Ofsted Region") or "").strip() or None,
                    government_region=(
                        str(row.get("Government Office Region") or "").strip() or None
                    ),
                    sector=str(row.get("Sector") or "").strip() or None,
                    places=_integer(row.get("Places")),
                    latest_event_type=str(row.get("Event type") or "").strip() or None,
                    latest_inspection_date=_parse_date(row.get("Inspection date")),
                    latest_publication_date=_parse_date(row.get("Publication date")),
                    source_url=source_url,
                    raw=row,
                )
                yielded += 1
                if yielded >= query.max_records:
                    return
            elif event == "end" and element.tag == f"{{{TABLE_NS}}}table":
                current_sheet = None
                element.clear()


def ofsted_signal(record: OfstedRecord, *, retrieved_at: datetime | None = None) -> dict[str, Any]:
    retrieved_at = retrieved_at or datetime.now(UTC)
    location = record.local_authority or record.ofsted_region
    metadata = {
        "provider": "OFSTED",
        "ofsted_urn": record.urn,
        "registration_status": record.registration_status,
        "registration_date": (
            record.registration_date.isoformat() if record.registration_date else None
        ),
        "local_authority": record.local_authority,
        "ofsted_region": record.ofsted_region,
        "government_region": record.government_region,
        "sector": record.sector,
        "places": record.places,
        "latest_event_type": record.latest_event_type,
        "latest_inspection_date": (
            record.latest_inspection_date.isoformat() if record.latest_inspection_date else None
        ),
        "latest_publication_date": (
            record.latest_publication_date.isoformat() if record.latest_publication_date else None
        ),
        "location_sensitivity": "INTERNAL_EXACT",
        "ofsted_location_redacted": True,
        "dataset_url": DEFAULT_OFSTED_DATA_URL,
    }
    facts = [
        f"Registration status: {record.registration_status}",
        f"Registration date: {metadata['registration_date'] or 'not published'}",
        f"Local authority: {record.local_authority or 'not published'}",
        f"Registered provider: {record.provider_name}",
    ]
    return {
        "schema_version": "1.0",
        "vertical": "CHILDRENS_HOME",
        "source_type": "ofsted",
        "source_url": record.source_url,
        "external_id": f"ofsted:{record.urn}",
        "discovered_at": retrieved_at.isoformat(),
        "title": f"Ofsted registration — {record.provider_name}",
        "raw_text": ". ".join(facts),
        "location_hint": location,
        "organisation_hint": record.provider_name,
        "metadata": metadata,
    }
