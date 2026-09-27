from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.care import enrich_care_signal
from app.procurement import (
    CONTRACTS_FINDER,
    FIND_A_TENDER,
    ProcurementQuery,
    classify_procurement,
    iter_evaluation_records,
    iter_releases,
    procurement_signal,
    same_procurement_process,
)


def release(description: str, *, tag: str = "tender", awards: list | None = None) -> dict:
    return {
        "ocid": "ocds-test-1",
        "id": "release-1",
        "date": "2025-06-01T09:00:00Z",
        "tag": [tag],
        "buyer": {"id": "buyer-1", "name": "Example Council"},
        "tender": {
            "title": "Children's services procurement",
            "description": description,
            "datePublished": "2025-06-01T09:00:00Z",
            "documents": [{"url": "https://example.gov.uk/notices/1"}],
        },
        "awards": awards or [],
        "parties": [
            {
                "id": "buyer-1",
                "name": "Example Council",
                "roles": ["buyer"],
                "address": {"locality": "Coventry", "postalCode": "CV1 1AA"},
                "contactPoint": {
                    "name": "A Person",
                    "email": "person@example.gov.uk",
                    "telephone": "0123456789",
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("description", "category"),
    [
        (
            "The council will commission three new children's homes to create additional capacity.",
            "NEW_HOME_COMMISSIONING",
        ),
        (
            "We are seeking a provider to operate two new council-owned children's homes.",
            "OPERATOR_PROCUREMENT",
        ),
        (
            "Soft market engagement to establish additional new children's homes.",
            "NEW_CAPACITY_MARKET_ENGAGEMENT",
        ),
        (
            "Framework for spot purchase placements in residential children's homes.",
            "ROUTINE_PLACEMENT_FRAMEWORK",
        ),
        (
            "Renewal of the existing children's home service contract.",
            "EXISTING_SERVICE_REPROCUREMENT",
        ),
        ("Framework for adult residential care and supported living.", "IRRELEVANT"),
    ],
)
def test_procurement_classification(description: str, category: str) -> None:
    assert classify_procurement(release(description))["category"] == category


def test_new_home_award_is_contract_award() -> None:
    value = release(
        "Contract award for operation of a newly created children's home.",
        tag="award",
        awards=[
            {
                "id": "award-1",
                "title": "New home award",
                "suppliers": [{"id": "supplier-1", "name": "Acme Care Ltd"}],
            }
        ],
    )
    assert classify_procurement(value)["category"] == "CONTRACT_AWARD"


def test_council_property_future_registration_award_is_contract_award() -> None:
    value = release(
        "The contract is to commission an external provider to operate a residential "
        "children's home using the Council's property. It will be Ofsted-registered.",
        tag="award",
        awards=[{"id": "award-1", "suppliers": [{"name": "Acme Care Ltd"}]}],
    )
    assert classify_procurement(value)["category"] == "CONTRACT_AWARD"


def test_ambiguous_childrens_home_notice_stays_uncertain() -> None:
    result = classify_procurement(
        release("Market information relating to children's residential homes in the region.")
    )
    assert result["category"] == "UNCERTAIN"
    assert result["strong_candidate"] is False


def test_new_childrens_homes_framework_is_not_mistaken_for_physical_new_homes() -> None:
    result = classify_procurement(
        release(
            "YPO is exploring the establishment of a new values-based Children's Homes "
            "Framework to secure stable placements and strategic commissioning."
        )
    )
    assert result["category"] == "ROUTINE_PLACEMENT_FRAMEWORK"


def test_normalisation_removes_personal_contact_but_retains_buyer_and_process() -> None:
    signal, raw = procurement_signal(
        FIND_A_TENDER,
        release("The council will commission a new children's home."),
    )
    assert signal["metadata"]["ocid"] == "ocds-test-1"
    assert signal["organisation_hint"] == "Example Council"
    assert signal["metadata"]["evaluation_mode"] == "SHADOW_ONLY"
    assert "contactPoint" not in raw["parties"][0]


def test_procurement_enrichment_can_never_create_live_opportunity() -> None:
    signal, _ = procurement_signal(
        CONTRACTS_FINDER,
        release("Seeking a provider to operate a new council-owned children's home."),
    )
    candidate = enrich_care_signal({"id": "signal-1", **signal})
    assert candidate["extracted_facts"]["opportunity_creation_decision"] == "REVIEW"
    assert candidate["extracted_facts"]["procurement_evaluation_mode"] == "SHADOW_ONLY"


def test_release_pagination_is_bounded_and_deterministic() -> None:
    urls: list[str] = []

    def fetch(url: str) -> dict:
        urls.append(url)
        if len(urls) == 1:
            return {
                "releases": [release("one"), {**release("two"), "id": "release-2"}],
                "links": {"next": "https://example.gov.uk/page-2"},
            }
        return {"releases": [{**release("three"), "id": "release-3"}], "links": {}}

    values = list(
        iter_releases(
            FIND_A_TENDER,
            ProcurementQuery(31, 2, 100),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            fetch=fetch,
        )
    )
    assert [item["id"] for item in values] == ["release-1", "release-2"]
    assert len(urls) == 1


def test_release_identity_is_stable_across_amendments() -> None:
    first, _ = procurement_signal(FIND_A_TENDER, release("Initial notice"))
    amended, _ = procurement_signal(FIND_A_TENDER, release("Amended notice"))
    assert first["external_id"] == amended["external_id"]


def test_award_correlates_to_earlier_notice_only_by_shared_ocid() -> None:
    tender = release("Commission a new children's home")
    award = {**release("Award to operate the new children's home", tag="award"), "id": "award-1"}
    unrelated = {**award, "ocid": "ocds-other"}
    assert same_procurement_process(tender, award) is True
    assert same_procurement_process(tender, unrelated) is False


def test_targeted_evaluation_records_are_official_and_bounded() -> None:
    urls: list[str] = []

    def fetch(url: str) -> dict:
        urls.append(url)
        return {"releases": [release("Commission a new children's home")]}

    values = list(iter_evaluation_records(fetch=fetch))
    assert len(values) == 5
    assert len(urls) == 5
    assert all(
        "find-tender.service.gov.uk" in url
        or "contractsfinder.service.gov.uk" in url
        for url in urls
    )
