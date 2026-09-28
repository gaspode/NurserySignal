from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from app.care import enrich_care_signal
from app.config import Settings
from app.ofsted import OfstedQuery, ofsted_signal, records_from_ods
from app.ofsted_collector import collect_ofsted
from app.ofsted_enrichment import (
    enrich_ofsted_urn,
    parse_provider_page,
    parse_report_pdf,
    persist_ofsted_urn_enrichment,
)
from app.repository import _postgres_text, _validated_organisation_name


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _provider_page(*, reports: bool = True) -> bytes:
    report_rows = ""
    if reports:
        report_rows = """
        <li class="timeline__day"><div class="event">
          <p class="timeline__date"><time>28 July 2026</time></p>
          <a class="publication-link" href="https://files.ofsted.gov.uk/v1/file/50311430">
            Full inspection <span class="nonvisual">Full inspection, PDF - 08 September 2026</span>
          </a><div>Published <time>08 September 2026</time></div>
        </div></li>
        <li class="timeline__day"><div class="event">
          <p class="timeline__date"><time>27 November 2025</time></p>
          <a class="publication-link" href="https://files.ofsted.gov.uk/v1/file/50293150">
            Monitoring visit <span class="nonvisual">Monitoring visit, PDF - 31 December 2025</span>
          </a><div>Published <time>31 December 2025</time></div>
        </div></li>
        """
    return f"""
      <html><head><link rel="canonical" href="https://reports.ofsted.gov.uk/provider/2/2766766"></head>
      <body><ol>{report_rows}<li class="timeline__day timeline__day--registered">
        <time>04 October 2024</time><span>Registration</span></li></ol>
        <ul><li><span>Type: </span><span>Children's Home</span></li>
        <li><span>Local authority: </span><span>Lancashire</span></li></ul>
      </body></html>
    """.encode()


def _tagged_report() -> bytes:
    return (
        b"%PDF-1.7 /ActualText(Registered provider: ) "
        b"/ActualText(Oaktree Childcare Limited) "
        b"/ActualText(Registered provider address: ) "
        b"/ActualText(Ground Floor, Seneca House, Links Point, Amy Johnson Way, "
        b"Blackpool, Lancashire FY4 2FF) /ActualText(Responsible individual: ) "
        b"/ActualText(A person who is deliberately not retained)"
    )


def _tagged_report_without_county() -> bytes:
    return (
        b"%PDF-1.7 /ActualText(Registered provider: ) "
        b"/ActualText(Within Reach Services Limited) "
        b"/ActualText(Registered provider address: ) "
        b"/ActualText(c/o Hazlewoods LLP, Windsor House, Bayshill Road, "
        b"Cheltenham GL50 3AT) /ActualText(Responsible individual: )"
    )


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


def test_known_urn_page_and_report_extract_provider_identity_without_home_address() -> None:
    page = parse_provider_page(_provider_page(), "2766766")
    report = parse_report_pdf(_tagged_report())

    assert page == {
        "urn": "2766766",
        "provider_page_url": "https://reports.ofsted.gov.uk/provider/2/2766766",
        "provision_type": "Children's Home",
        "local_authority": "Lancashire",
        "registration_date": "2024-10-04",
        "latest_report_type": "Full inspection",
        "latest_report_date": "2026-07-28",
        "latest_report_publication_date": "2026-09-08",
        "latest_report_url": "https://files.ofsted.gov.uk/v1/file/50311430",
        "report_count": 2,
    }
    assert report["registered_provider_name"] == "Oaktree Childcare Limited"
    assert report["provider_registered_locality"] == "Blackpool"
    assert report["provider_registered_region"] == "Lancashire"
    assert report["provider_registered_postcode"] == "FY4 2FF"
    assert "home_address" not in report
    assert "manager" not in report

    no_county = parse_report_pdf(_tagged_report_without_county())
    assert no_county["provider_registered_locality"] == "Cheltenham"
    assert no_county["provider_registered_region"] is None
    assert no_county["provider_registered_postcode"] == "GL50 3AT"


def test_urn_enrichment_uses_latest_public_report_and_handles_no_report() -> None:
    requested = []

    def opener(request, timeout):
        requested.append(request.full_url)
        if "files.ofsted.gov.uk" in request.full_url:
            return Response(_tagged_report())
        return Response(_provider_page())

    result = enrich_ofsted_urn(
        "2766766",
        opener=opener,
        retrieved_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    assert requested == [
        "https://reports.ofsted.gov.uk/provider/2/2766766",
        "https://files.ofsted.gov.uk/v1/file/50311430",
    ]
    assert result["status"] == "SUCCEEDED"
    assert result["registered_provider_name"] == "Oaktree Childcare Limited"

    no_report = enrich_ofsted_urn(
        "2766766",
        opener=lambda request, timeout: Response(_provider_page(reports=False)),
    )
    assert no_report["status"] == "NO_REPORT"
    assert no_report["latest_report_url"] is None


def test_urn_enrichment_evidence_identity_ignores_retrieval_time(monkeypatch) -> None:
    stored = []
    uploaded = []
    monkeypatch.setattr(
        "app.ofsted_enrichment.put_raw_evidence",
        lambda settings, bucket, key, payload: uploaded.append((key, payload)),
    )
    monkeypatch.setattr(
        "app.ofsted_enrichment.store_ofsted_urn_enrichment",
        lambda settings, **values: stored.append(values) or len(stored) == 1,
    )
    base = {
        **parse_provider_page(_provider_page(), "2766766"),
        **parse_report_pdf(_tagged_report()),
        "status": "SUCCEEDED",
        "parser_version": "ofsted-urn-v2",
    }
    settings = Settings(evidence_bucket="private-evidence")
    assert persist_ofsted_urn_enrichment(
        settings, "signal-1", {**base, "retrieved_at": "2026-09-27T10:00:00Z"}
    )
    assert not persist_ofsted_urn_enrichment(
        settings, "signal-1", {**base, "retrieved_at": "2026-09-27T11:00:00Z"}
    )
    assert uploaded[0][0] == uploaded[1][0]
    assert uploaded[0][1] == uploaded[1][1]
    assert stored[0]["content_sha256"] == stored[1]["content_sha256"]


def test_normalized_ofsted_text_removes_nul_without_rewriting_source_value() -> None:
    source = "Provider\x00 Office"
    assert _postgres_text(source) == "Provider Office"
    assert source == "Provider\x00 Office"


def test_run_on_ofsted_provider_text_is_not_used_as_organisation_identity() -> None:
    malformed = "Provider name " + ("report text " * 400)
    assert _validated_organisation_name(malformed) == ""
    assert _validated_organisation_name("Example Care Limited") == "Example Care Limited"


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
    collect_ofsted(
        settings, {"source": "manual", "max_records": 1, "urn_enrichment_limit": 0}
    )
    collect_ofsted(
        settings, {"source": "manual", "max_records": 1, "urn_enrichment_limit": 0}
    )

    assert len(queued) == 2
    assert queued[0].raw_provider_record == queued[1].raw_provider_record
    assert "retrieved_at" not in queued[0].raw_provider_record
    assert queued[0].raw_provider_record["dataset_url"] == settings.ofsted_data_url
    assert datetime.fromisoformat(queued[0].signal["discovered_at"]).tzinfo == UTC


def test_failed_urn_lookup_does_not_break_base_ofsted_collection(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "register.ods"
    _ods(path)
    queued = []
    monkeypatch.setattr("app.ofsted_collector.download_register", lambda _url: path)
    monkeypatch.setattr(
        "app.ofsted_collector.enrich_ofsted_urn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    monkeypatch.setattr(
        "app.ofsted_collector.send_ingestion_message",
        lambda _settings, message: queued.append(message),
    )
    result = collect_ofsted(
        Settings(
            ingestion_queue_url="https://sqs.example/ingestion",
            ofsted_data_url="https://example.test/ofsted.ods",
        ),
        {"source": "manual", "max_records": 1, "registered_since_days": 3650},
    )
    assert result["signals_queued"] == 1
    assert result["urn_enrichment_failed"] == 1
    assert queued[0].ofsted_urn_enrichment["status"] == "FAILED"
    assert queued[0].ofsted_urn_enrichment["failure_category"] == "TimeoutError"
