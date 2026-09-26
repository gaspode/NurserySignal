from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

import pytest
from app.config import Settings
from app.correlation import classify_match, deterministic_match, recruitment_evidence_strength
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


def test_gov_addresses_shape_preserves_teaching_assistant_postcode() -> None:
    record = normalize_gov_vacancy(
        {
            "vacancyReference": "VAC-TA-1",
            "title": "Teaching Assistant Apprentice",
            "description": (
                "Stephenson Way Academy & Nursery School offer the opportunity "
                "for achievement and development."
            ),
            "employer": {"name": "TUDHOE LEARNING TRUST"},
            "addresses": [{"addressLine1": "Stephenson Way", "postcode": "DL5 7DD"}],
            "course": {"route": "Education and early years", "title": "Teaching assistant"},
        },
        "https://api.apprenticeships.education.gov.uk/vacancies",
    )
    assert record.address == "Stephenson Way"
    assert record.postcode == "DL5 7DD"
    decision = classify_recruitment(record)
    assert decision["role_category"] == "teaching_assistant"
    assert "school_with_nursery" in decision["setting_categories"]
    assert decision["relevance"] == "RELEVANT_ROUTINE"
    assert decision["commercial_change_evidence"] == "NONE"
    assert decision["confidence"] < 0.8


def test_recruitment_role_and_setting_evidence_are_separate() -> None:
    teaching_assistant = classify_recruitment(
        vacancy(
            "Teaching Assistant Apprentice",
            "Stephenson Way Academy & Nursery School support pupils.",
        )
    )
    early_years = classify_recruitment(vacancy("Early Years Apprentice"))
    educator = classify_recruitment(vacancy("Early Years Educator"))
    childcare_apprenticeship = classify_recruitment(vacancy("Level 3 Childcare Apprenticeship"))
    generic_ta = classify_recruitment(
        replace(
            vacancy("Teaching Assistant Apprentice", "A mainstream secondary school role."),
            employer_name="Secondary School Trust",
            workplace_name="Secondary School",
        )
    )
    assert teaching_assistant["role_category"] == "teaching_assistant"
    assert teaching_assistant["matched"] is True
    assert early_years["role_category"] == "childcare_apprentice"
    assert educator["role_category"] == "early_years_educator"
    assert childcare_apprenticeship["role_category"] == "childcare_apprentice"
    assert generic_ta["matched"] is False
    assert generic_ta["relevance"] == "IRRELEVANT"


@pytest.mark.parametrize(
    ("title", "description", "relevance", "change"),
    [
        ("Nursery Practitioner", "Established nursery setting.", "RELEVANT_ROUTINE", "NONE"),
        ("Nursery Manager", "Join our established nursery.", "RELEVANT_ROUTINE", "WEAK"),
        ("Nursery Manager", "Join our new nursery opening soon.", "RELEVANT_CHANGE", "STRONG"),
    ],
)
def test_relevance_and_change_evidence_are_explicit(
    title: str, description: str, relevance: str, change: str
) -> None:
    decision = classify_recruitment(vacancy(title, description))
    assert decision["relevance"] == relevance
    assert decision["commercial_change_evidence"] == change


def test_recruitment_signal_preserves_coordinates_and_classification() -> None:
    record = normalize_gov_vacancy(
        {
            "vacancyReference": "VAC-GEO",
            "title": "Early Years Educator",
            "description": "A role in our nursery.",
            "addresses": [
                {
                    "addressLine1": "1 High Street",
                    "postcode": "OX28 4XX",
                    "latitude": 51.78,
                    "longitude": -1.49,
                }
            ],
        },
        "https://api.apprenticeships.education.gov.uk/vacancies",
    )
    signal = recruitment_signal(record, classify_recruitment(record))
    assert signal["metadata"]["postcode"] == "OX28 4XX"
    assert signal["metadata"]["latitude"] == 51.78
    assert signal["metadata"]["recruitment_classification"]["relevance"] == "RELEVANT_ROUTINE"


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
    requests = []

    def opener(request, timeout):
        requests.append(request)
        page = 1 if len(requests) == 1 else 2
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
    assert "X-Version=2" not in requests[0].full_url  # version is deliberately a header
    assert requests[0].headers["X-version"] == "2"
    assert requests[0].headers["Accept"] == "application/json"
    assert requests[0].headers["Ocp-apim-subscription-key"] == "key"
    assert requests[0].headers["User-agent"] == "NurserySignal/1.0"


def test_gov_provider_reports_bounded_http_error_without_api_key(caplog) -> None:
    api_key = "secret-api-key"

    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            403,
            "forbidden",
            {"Content-Type": "application/json"},
            BytesIO(json.dumps({"message": "invalid key", "echo": api_key}).encode()),
        )

    provider = GovApprenticeshipProvider(api_key, opener=opener, sleep=lambda _: None)
    with pytest.raises(Exception, match="HTTP 403") as caught:
        list(provider.vacancies(RecruitmentQuery(max_records=1)))

    assert "invalid key" in str(caught.value)
    assert api_key not in str(caught.value)
    assert api_key not in caplog.text


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


def test_routine_recruitment_is_weak_corroboration_and_change_is_stronger() -> None:
    routine = {"extracted_facts": {"recruitment_relevance": "RELEVANT_ROUTINE"}}
    change = {
        "extracted_facts": {
            "recruitment_relevance": "RELEVANT_CHANGE",
            "commercial_change_evidence": "STRONG",
        }
    }
    uncertain = {"extracted_facts": {"recruitment_relevance": "UNCERTAIN"}}
    assert recruitment_evidence_strength(routine)[0] == 0.05
    assert recruitment_evidence_strength(change)[0] == 0.18
    assert recruitment_evidence_strength(uncertain)[0] == 0.0


def test_generic_match_outcomes_are_explainable_and_conservative() -> None:
    exact = classify_match("OX28 4XX", "Little Acorns Nursery", "OX28 4XX", "Little Acorns")
    assert (exact.outcome, exact.confidence) == ("EXACT", 0.98)
    uncertain = classify_match("OX28 4XX", "Little Acorns", "OX28 4XX", "Other Nursery")
    assert uncertain.outcome == "UNCERTAIN"
    assert (
        classify_match("OX28 4XX", "Little Acorns", "OX29 1AA", "Little Acorns").outcome
        == "NO_MATCH"
    )
