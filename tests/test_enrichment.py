from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from app.enrichment import fixture_enrichment
from app.worker import handler


def raw_signal(
    text: str, source_type: str = "planning", title: str = "New nursery planning application"
) -> dict:
    return {
        "id": uuid4(),
        "schema_version": "1.0",
        "source_type": source_type,
        "source_url": "https://example.test/source",
        "title": title,
        "raw_text": text,
        "location_hint": "Bristol BS1",
        "organisation_hint": "Little Acorns",
        "metadata": {"capacity": 42},
    }


def test_fixture_enrichment_extracts_candidate() -> None:
    candidate = fixture_enrichment(raw_signal("A proposed nursery planning application."))
    assert candidate["event_type"] == "opening"
    assert candidate["lifecycle_stage"] == "PLANNING"
    assert candidate["capacity"] == 42
    assert candidate["extracted_facts"]["method"] == "fixture-v1"


def test_fixture_enrichment_rejects_false_positive_semantically() -> None:
    candidate = fixture_enrichment(
        raw_signal(
            "A garden nursery won an ecology award.",
            "local_news",
            "Community garden wins an award",
        )
    )
    assert candidate["event_type"] == "other"
    assert candidate["confidence"] < 0.5


def test_fixture_enrichment_honours_explicit_false_positive_negation() -> None:
    candidate = fixture_enrichment(
        raw_signal(
            "The community garden nursery has won an award for its biodiversity project; "
            "no childcare opening is planned.",
            "local_news",
            "Community garden nursery wins award",
        )
    )
    assert candidate["event_type"] == "other"
    assert candidate["lifecycle_stage"] == "DISCOVERED"


@pytest.mark.parametrize(
    "text",
    [
        "The community garden nursery won an award for its biodiversity project.",
        "A community garden nursery wins a horticultural award for growing plants.",
    ],
)
def test_observed_community_garden_variants_are_consistently_false_positive(text: str) -> None:
    candidate = fixture_enrichment(
        raw_signal(text, "local_news", "Community garden nursery wins award")
    )
    assert candidate["event_type"] == "other"
    assert candidate["lifecycle_stage"] == "DISCOVERED"
    assert candidate["confidence"] < 0.5
    assert candidate["extracted_facts"]["likely_false_positive"] is True
    assert candidate["extracted_facts"]["classification"] == "horticultural-nursery"


def test_horticultural_guardrail_runs_before_planning_promotion() -> None:
    candidate = fixture_enrichment(
        raw_signal(
            "Planning application for a plant nursery expansion and nursery stock.",
            "planning",
            "Plant nursery expansion",
        )
    )
    assert candidate["event_type"] == "other"
    assert candidate["lifecycle_stage"] == "DISCOVERED"
    assert candidate["extracted_facts"]["likely_false_positive"] is True


def test_explicit_childcare_evidence_overrides_horticultural_context() -> None:
    candidate = fixture_enrichment(
        raw_signal(
            "A day nursery will include a small garden where children grow plants.",
            "planning",
            "Children's day nursery opening",
        )
    )
    assert candidate["event_type"] == "opening"
    assert candidate["lifecycle_stage"] == "PLANNING"
    assert candidate["extracted_facts"]["likely_false_positive"] is False


def test_classification_regression_fixtures_drive_enrichment() -> None:
    fixtures = json.loads(
        Path("fixtures/classification_regressions.json").read_text(encoding="utf-8")
    )
    for fixture in fixtures:
        candidate = fixture_enrichment(
            raw_signal(
                fixture["raw_text"],
                fixture["source_type"],
                fixture["title"],
            )
        )
        is_false_positive = fixture["expected"] == "false_positive"
        assert candidate["extracted_facts"]["likely_false_positive"] is is_false_positive
        if is_false_positive:
            assert candidate["event_type"] == "other"
            assert candidate["lifecycle_stage"] == "DISCOVERED"
        else:
            assert candidate["event_type"] == "opening"


def test_poison_message_is_reported_for_dlq(monkeypatch) -> None:
    monkeypatch.setattr("app.worker.Settings.from_env", lambda: object())
    result = handler({"Records": [{"messageId": "poison-1", "body": "not-json"}]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "poison-1"}]}


def test_worker_processes_valid_message(monkeypatch) -> None:
    raw = raw_signal("A new nursery is proposed.")
    message = {
        "message_version": "1.0",
        "signal_id": str(raw["id"]),
        "schema_version": "1.0",
        "evidence_bucket": "evidence",
        "evidence_key": "signals/test/raw.json",
        "queued_at": "2026-09-24T08:00:00Z",
    }
    saved = []
    monkeypatch.setattr("app.worker.Settings.from_env", lambda: object())
    monkeypatch.setattr("app.worker.get_raw_signal", lambda settings, value: raw)
    monkeypatch.setattr(
        "app.worker.save_enrichment", lambda settings, value: saved.append(value) or True
    )
    result = handler({"Records": [{"messageId": "good-1", "body": json.dumps(message)}]}, None)
    assert result == {"batchItemFailures": []}
    assert saved[0]["raw_signal_id"] == raw["id"]
