from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.config import Settings
from app.correlation import deterministic_match
from app.recruitment import (
    GovApprenticeshipProvider,
    RecruitmentQuery,
    RecruitmentRecord,
    classify_recruitment,
    normalize_gov_vacancy,
    recruitment_signal,
)
from app.recruitment_collector import collect_recruitment


def vacancy(title: str, description: str = "A role at a childcare setting") -> RecruitmentRecord:
    return RecruitmentRecord(
        provider="govuk-apprenticeships",
        external_id="VAC-1",
        source_url="https://example.test/vacancy/VAC-1",
        title=title,
        employer_name="Little Acorns",
        workplace_name="Little Acorns Nursery",
        address="1 High Street",
        postcode="OX28 4XX",
        locality="Witney",
        region="Oxfordshire",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        expires_at=None,
        salary=None,
        description=description,
        employment_type="Permanent",
        raw={"vacancyReference": "VAC-1", "title": title},
    )


@pytest.mark.parametrize(
    "title", ["Nursery Manager", "Early Years Practitioner", "Room Leader", "Childcare Apprentice"]
)
def test_childcare_roles_match(title: str) -> None:
    assert classify_recruitment(vacancy(title))["matched"] is True


def test_explicit_new_setting_is_stronger_than_routine_vacancy() -> None:
    routine = classify_recruitment(vacancy("Nursery Practitioner"))
    new = classify_recruitment(
        vacancy("Nursery Manager", "Join our new nursery setting opening soon.")
    )
    assert new["confidence"] > routine["confidence"]
    assert "new_setting" in new["explicit_change_terms"]


@pytest.mark.parametrize(
    "title", ["Plant Nursery Manager", "Tree Nursery Stock Worker", "NHS Nursery Nurse"]
)
def test_non_childcare_roles_are_excluded(title: str) -> None:
    assert classify_recruitment(vacancy(title))["matched"] is False


def test_normalization_preserves_reference_and_location() -> None:
    record = normalize_gov_vacancy(
        {
            "vacancyReference": "VAC-99",
            "title": "Nursery Manager",
            "description": "New setting",
            "employer": {"name": "Little Acorns"},
            "locations": [{"postcode": "OX28 4XX", "town": "Witney"}],
        },
        "https://api.apprenticeships.education.gov.uk/vacancies",
    )
    assert record.external_id == "VAC-99"
    assert record.postcode == "OX28 4XX"
    assert (
        recruitment_signal(record, classify_recruitment(record))["external_id"]
        == "govuk-apprenticeships:VAC-99"
    )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_gov_provider_paginates_bounded_results() -> None:
    urls = []

    def opener(request, timeout):
        urls.append(request.full_url)
        page = 1 if len(urls) == 1 else 2
        return FakeResponse(
            {
                "vacancies": [{"vacancyReference": f"VAC-{page}", "title": "Nursery Manager"}]
                if page == 1
                else []
            }
        )

    provider = GovApprenticeshipProvider("key", opener=opener, sleep=lambda _: None)
    assert [
        item.external_id
        for item in provider.vacancies(RecruitmentQuery(max_records=2, page_size=1))
    ] == ["VAC-1"]
    assert "X-Version=2" not in urls[0]  # version is deliberately a header


def test_recruitment_collector_queues_only_childcare(monkeypatch) -> None:
    queued = []

    class Provider:
        def vacancies(self, query):
            yield vacancy("Nursery Manager")
            yield vacancy("Plant Nursery Manager")

    monkeypatch.setattr(
        "app.recruitment_collector.send_ingestion_message",
        lambda settings, message: queued.append(message),
    )
    counts = collect_recruitment(
        Settings(ingestion_queue_url="https://sqs.example/q"),
        {"source": "manual", "max_records": 10},
        Provider(),
    )
    assert counts["records_fetched"] == 2
    assert counts["candidates_matched"] == 1
    assert counts["excluded"] == 1
    assert queued[0].signal["source_type"] == "recruitment"


def test_correlation_requires_postcode_and_compatible_name() -> None:
    assert deterministic_match(
        "OX28 4XX", "Little Acorns Nursery", "OX28 4XX", "Little Acorns"
    ).matched
    assert not deterministic_match(
        "OX28 4XX", "Little Acorns Nursery", "OX29 1AA", "Little Acorns"
    ).matched
    assert not deterministic_match(
        "OX28 4XX", "Little Acorns Nursery", "OX28 4XX", "Other Nursery"
    ).matched
