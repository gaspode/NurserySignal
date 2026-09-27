from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from app.care import enrich_care_signal
from app.config import Settings
from app.ofsted import OfstedQuery, ofsted_signal, records_from_ods
from app.ofsted_collector import collect_ofsted


def _ods(path: Path) -> None:
    headers = [
        "Web link",
        "URN",
        "Provision type",
        "Registration date",
        "Registration status",
        "Name",
        "Address 1",
        "Town",
        "Postcode",
        "Ofsted Region",
        "Government Office Region",
        "Local authority",
        "Sector",
        "Places",
        "Organisation which owns the provider",
        "Event type",
        "Inspection date",
        "Publication date",
    ]
    values = [
        "report",
        "SC123456",
        "Children's home",
        "01/06/2025",
        "Active",
        "REDACTED",
        "REDACTED",
        "REDACTED",
        "REDACTED",
        "North East, Yorkshire and The Humber",
        "North East",
        "Durham",
        "Private",
        "4",
        "Example Care Limited",
        "Full inspection",
        "01/08/2025",
        "14/08/2025",
    ]

    def row(items: list[str], *, link: bool = False) -> str:
        cells = []
        for index, item in enumerate(items):
            value = (
                f'<text:a xlink:href="https://reports.ofsted.gov.uk/provider/2/SC123456">{item}</text:a>'
                if link and index == 0
                else item
            )
            cells.append(f"<table:table-cell><text:p>{value}</text:p></table:table-cell>")
        return f"<table:table-row>{''.join(cells)}</table:table-row>"

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <office:document-content
      xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
      xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
      xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
      xmlns:xlink="http://www.w3.org/1999/xlink">
      <office:body><office:spreadsheet>
        <table:table table:name="Provider_level_at_30_09_25">
          {row(headers)}{row(values, link=True)}
        </table:table>
      </office:spreadsheet></office:body>
    </office:document-content>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", content)


def test_official_register_normalization_preserves_redaction(tmp_path: Path) -> None:
    path = tmp_path / "register.ods"
    _ods(path)
    records = list(
        records_from_ods(
            path,
            OfstedQuery(max_records=10, registered_since_days=365, active_only=True),
            today=date(2025, 9, 30),
        )
    )
    assert len(records) == 1
    record = records[0]
    assert record.urn == "SC123456"
    assert record.provider_name == "Example Care Limited"
    assert record.local_authority == "Durham"
    assert record.places == 4
    assert record.source_url.endswith("/SC123456")
    assert record.raw["Address 1"] == "REDACTED"

    signal = ofsted_signal(record)
    assert signal["vertical"] == "CHILDRENS_HOME"
    assert signal["external_id"] == "ofsted:SC123456"
    assert signal["location_hint"] == "Durham"
    assert signal["metadata"]["ofsted_location_redacted"] is True
    assert "REDACTED" not in signal["location_hint"]


def test_ofsted_is_supporting_regulatory_evidence_not_a_new_opportunity() -> None:
    candidate = enrich_care_signal(
        {
            "id": "signal-id",
            "source_type": "ofsted",
            "source_url": "https://reports.ofsted.gov.uk/provider/2/SC123456",
            "title": "Ofsted registration — Example Care Limited",
            "raw_text": "Registration status: Active",
            "location_hint": "Durham",
            "organisation_hint": "Example Care Limited",
            "metadata": {
                "ofsted_urn": "SC123456",
                "registration_status": "Active",
                "local_authority": "Durham",
            },
        }
    )
    assert candidate["lifecycle_stage"] == "REGISTRATION"
    assert candidate["extracted_facts"]["opportunity_creation_decision"] == (
        "SUPPORT_EXISTING_ONLY"
    )
    assert candidate["extracted_facts"]["likely_false_positive"] is False


def test_ofsted_repeated_collection_keeps_provider_evidence_stable(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "register.ods"
    _ods(path)
    records = list(
        records_from_ods(
            path,
            OfstedQuery(max_records=1, registered_since_days=365, active_only=True),
            today=date(2025, 9, 30),
        )
    )
    queued = []

    def download(_url: str) -> Path:
        copy = tmp_path / f"register-{len(queued)}.ods"
        _ods(copy)
        return copy

    monkeypatch.setattr("app.ofsted_collector.download_register", download)
    monkeypatch.setattr("app.ofsted_collector.records_from_ods", lambda *_: iter(records))
    monkeypatch.setattr(
        "app.ofsted_collector.send_ingestion_message",
        lambda _settings, message: queued.append(message),
    )

    settings = Settings(
        ingestion_queue_url="https://sqs.example/ingestion",
        ofsted_data_url="https://example.test/ofsted.ods",
    )
    collect_ofsted(settings, {"source": "manual", "max_records": 1})
    collect_ofsted(settings, {"source": "manual", "max_records": 1})

    assert len(queued) == 2
    assert queued[0].raw_provider_record == queued[1].raw_provider_record
    assert "retrieved_at" not in queued[0].raw_provider_record
    assert queued[0].raw_provider_record["dataset_url"] == settings.ofsted_data_url
    assert datetime.fromisoformat(queued[0].signal["discovered_at"]).tzinfo == UTC
