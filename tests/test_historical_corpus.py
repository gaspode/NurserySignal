from __future__ import annotations

import json
from pathlib import Path

from app.historical_corpus import evidence_eligibility, record_content_hash, replay_signal


def historical_record(**overrides):
    value = {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "corpus_version": "care-historical-research-v1",
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "provider": "PLANNING_INSPECTORATE",
        "external_id": "APP/G4620/W/23/3328400",
        "source_url": "https://acp.planninginspectorate.gov.uk/ViewCase.aspx?caseid=3328400",
        "title": "Change of use to a children's residential home",
        "raw_text": "Change of use from dwelling to children's residential home.",
        "organisation_hint": "KDB Care Ltd",
        "location_hint": "Oldbury, Sandwell",
        "available_at": "2024-02-01T00:00:00Z",
        "retrieved_at": "2026-09-27T00:00:00Z",
        "metadata": {"publication_date": "2024-02-01", "postcode": "B69 1LZ"},
        "provenance": {"source_kind": "official appeal decision"},
    }
    value.update(overrides)
    return value


def test_official_dated_planning_item_is_replay_eligible() -> None:
    assert (
        evidence_eligibility(
            outcome_at="2025-01-14",
            available_at="2024-02-01",
            source_url="https://acp.planninginspectorate.gov.uk/ViewCase.aspx?caseid=3328400",
            provider="PLANNING_INSPECTORATE",
            case_link_confidence="STRONG_CASE_LINK",
        )
        == "ELIGIBLE"
    )


def test_undated_page_and_post_registration_recruitment_are_excluded() -> None:
    assert (
        evidence_eligibility(
            outcome_at="2025-01-14",
            available_at=None,
            source_url="https://www.warwickshire.gov.uk/news/article/5704/support-worker",
            provider="WARWICKSHIRE_COUNCIL",
            case_link_confidence="STRONG_CASE_LINK",
        )
        == "EXCLUDED_NO_RELIABLE_PUBLICATION_DATE"
    )
    assert (
        evidence_eligibility(
            outcome_at="2025-01-14",
            available_at="2025-01-15",
            source_url="https://www.warwickshire.gov.uk/news/article/5704/support-worker",
            provider="WARWICKSHIRE_COUNCIL",
            case_link_confidence="STRONG_CASE_LINK",
        )
        == "EXCLUDED_AFTER_REGISTRATION"
    )


def test_possible_case_link_is_not_admitted_to_replay() -> None:
    assert (
        evidence_eligibility(
            outcome_at="2025-01-14",
            available_at="2024-02-01",
            source_url="https://www.sefton.gov.uk/planning/example.pdf",
            provider="SEFTON_COUNCIL",
            case_link_confidence="POSSIBLE_CASE_LINK",
        )
        == "EXCLUDED_WEAK_CASE_LINK"
    )


def test_retrieval_timestamp_does_not_change_content_identity() -> None:
    first = historical_record(retrieved_at="2026-09-27T00:00:00Z")
    second = historical_record(retrieved_at="2026-09-28T00:00:00Z")
    assert record_content_hash(first) == record_content_hash(second)


def test_replay_projection_contains_source_facts_but_not_benchmark_truth() -> None:
    projected = replay_signal(
        historical_record(
            known_ofsted_urn="2813382",
            known_registration_date="2025-01-14",
        )
    )
    assert projected["external_id"] == "APP/G4620/W/23/3328400"
    assert projected["metadata"]["publication_date"] == "2024-02-01"
    assert "known_ofsted_urn" not in projected
    assert "known_registration_date" not in projected
    assert "ofsted_urn" not in projected["metadata"]


def test_bundled_corpus_is_bounded_unique_and_reproducibly_eligible() -> None:
    manifest = json.loads(
        Path("backend/app/data/care_historical_research_v1.json").read_text(encoding="utf-8")
    )
    assert len(manifest["cases"]) == 21
    assert len({item["benchmark_case_id"] for item in manifest["cases"]}) == 21
    identities = {
        (item["provider"], item["source_type"], item["external_id"]) for item in manifest["records"]
    }
    assert len(identities) == len(manifest["records"])
    records = {
        (item["provider"], item["source_type"], item["external_id"]): item
        for item in manifest["records"]
    }
    outcomes = {
        "ofsted:2780410": "2025-03-14",
        "ofsted:2813382": "2025-01-14",
        "ofsted:2820714": "2025-04-14",
        "ofsted:2817348": "2025-02-11",
        "ofsted:2816819": "2025-02-19",
        "ofsted:2819221": "2025-03-03",
        "ofsted:2823938": "2025-03-28",
    }
    for case_item in manifest["cases"]:
        for candidate in case_item["candidates"]:
            record = records[
                (candidate["provider"], candidate["source_type"], candidate["external_id"])
            ]
            calculated = evidence_eligibility(
                outcome_at=outcomes[case_item["benchmark_case_id"]],
                available_at=record["available_at"],
                source_url=record["source_url"],
                provider=record["provider"],
                case_link_confidence=candidate["case_link_confidence"],
            )
            assert calculated == candidate["eligibility"]
