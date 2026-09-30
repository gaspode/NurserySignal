from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from app.planning import PlanningRecord
from app.planning_origin import authorities_match, resolve_origin_candidate


def candidate(
    application_id: str,
    council: str,
    address: str,
    postcode: str,
) -> PlanningRecord:
    return PlanningRecord(
        provider="plota",
        application_id=application_id,
        application_url=f"https://planning.example/{application_id}",
        description="Change of use to a children's care home (Class C2)",
        address=address,
        postcode=postcode,
        latitude=None,
        longitude=None,
        application_date=date(2024, 1, 1),
        status="Decided",
        decision="Approved",
        decision_date=date(2024, 3, 1),
        council=council,
        applicant=None,
        agent=None,
        raw={"id": application_id, "reference": "24/03385/FUL"},
    )


def test_ashburton_same_reference_across_councils_selects_croydon() -> None:
    candidates = (
        candidate("sheffield", "Sheffield", "1 Other Road, Sheffield", "S1 1AA"),
        candidate("enfield", "Enfield", "2 Other Road, Enfield", "EN1 1AA"),
        candidate(
            "croydon", "London Borough of Croydon", "58 Ashburton Road Croydon", "CR0 6AN"
        ),
    )

    result = resolve_origin_candidate(
        candidates,
        authority="Croydon",
        postcode="CR0 6AN",
        address="58 Ashburton Road Croydon CR0 6AN",
        truncated=False,
    )

    assert result.status == "FOUND"
    assert result.selected.application_id == "croydon"
    assert result.reason == "unique_normalized_authority_match"


@pytest.mark.parametrize(
    ("left", "right"),
    [("Croydon", "London Borough of Croydon"), ("Trafford", "Trafford Council")],
)
def test_authority_aliases_match(left: str, right: str) -> None:
    assert authorities_match(left, right)


def test_zero_candidates_is_the_only_not_found_result() -> None:
    result = resolve_origin_candidate(
        (), authority="Croydon", postcode="CR0 6AN", address=None, truncated=False
    )
    assert result.status == "NOT_FOUND"


def test_single_exact_reference_candidate_is_found() -> None:
    item = candidate("one", "Sheffield", "1 Other Road", "S1 1AA")
    result = resolve_origin_candidate(
        (item,), authority="Croydon", postcode=None, address=None, truncated=False
    )
    assert result.status == "FOUND"


def test_candidates_without_context_match_are_ambiguous_not_not_found() -> None:
    result = resolve_origin_candidate(
        (
            candidate("one", "Sheffield", "1 Other Road", "S1 1AA"),
            candidate("two", "Enfield", "2 Other Road", "EN1 1AA"),
        ),
        authority="Croydon",
        postcode="CR0 6AN",
        address="58 Ashburton Road",
        truncated=False,
    )
    assert result.status == "AMBIGUOUS"
    assert result.reason == "no_context_match"


def test_exact_postcode_breaks_same_authority_tie() -> None:
    result = resolve_origin_candidate(
        (
            candidate("one", "Croydon Council", "1 Other Road", "CR0 1AA"),
            candidate("ashburton", "Croydon", "58 Ashburton Road", "CR0 6AN"),
        ),
        authority="London Borough of Croydon",
        postcode="CR0 6AN",
        address="58 Ashburton Road",
        truncated=False,
    )
    assert result.status == "FOUND"
    assert result.selected.application_id == "ashburton"
    assert result.reason == "unique_postcode_match"


def test_multiple_plausible_candidates_remain_ambiguous() -> None:
    first = candidate("one", "Croydon", "58 Ashburton Road", "CR0 6AN")
    result = resolve_origin_candidate(
        (first, replace(first, application_id="two")),
        authority="Croydon",
        postcode="CR0 6AN",
        address="58 Ashburton Road",
        truncated=False,
    )
    assert result.status == "AMBIGUOUS"


def test_truncated_candidate_set_is_never_selected() -> None:
    result = resolve_origin_candidate(
        (candidate("one", "Croydon", "58 Ashburton Road", "CR0 6AN"),),
        authority="Croydon",
        postcode="CR0 6AN",
        address="58 Ashburton Road",
        truncated=True,
    )
    assert result.status == "AMBIGUOUS"
    assert result.reason == "provider_candidate_set_truncated"
