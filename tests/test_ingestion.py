from datetime import UTC, datetime

import pytest
from app.ingestion import NormalizedSignal


def test_signal_normalizes_iso_timestamp_and_preserves_metadata() -> None:
    signal = NormalizedSignal.from_dict({
        "source_type": "planning",
        "source_url": "https://example.gov.uk/application/123",
        "external_id": "123",
        "discovered_at": "2026-09-23T10:00:00Z",
        "title": "Nursery planning application",
        "raw_text": "A new nursery is proposed.",
        "metadata": {"council": "Example"},
    })

    assert signal.discovered_at == datetime(2026, 9, 23, 10, tzinfo=UTC)
    assert signal.metadata["council"] == "Example"


def test_signal_rejects_non_http_source() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        NormalizedSignal.from_dict({
            "source_type": "planning",
            "source_url": "not-a-url",
            "discovered_at": "2026-09-23T10:00:00Z",
            "title": "Title",
            "raw_text": "Text",
        })

