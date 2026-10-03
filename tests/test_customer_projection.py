from __future__ import annotations

import pytest
from app.customer_projection import (
    customer_safe_location,
    customer_title,
    generated_customer_summary,
    generated_customer_title,
    postcode_district,
    safe_evidence_title,
)


@pytest.mark.parametrize(
    ("postcode", "expected"),
    [
        ("NG8 1LD", "NG8"),
        ("L5 4TN", "L5"),
        ("CV23 9FR", "CV23"),
        ("SW1A 1AA", "SW1A"),
        ("sw1a1aa", "SW1A"),
        (None, None),
        ("not a postcode", None),
    ],
)
def test_postcode_district_redacts_inward_code(postcode, expected) -> None:
    assert postcode_district(postcode) == expected


@pytest.mark.parametrize(
    ("town", "postcode", "expected"),
    [
        ("Nottingham", "NG8 1LD", "New children’s home — Nottingham, NG8"),
        ("Liverpool", "L5 4TN", "New children’s home — Liverpool, L5"),
        ("Rugby", "CV23 9FR", "New children’s home — Rugby, CV23"),
    ],
)
def test_opening_title_uses_town_and_postcode_district(town, postcode, expected) -> None:
    assert (
        generated_customer_title({"town": town, "postcode": postcode, "change_type": "OPENING"})
        == expected
    )


def test_location_uses_authority_then_region_when_town_is_unavailable() -> None:
    assert (
        customer_safe_location(
            local_authority="Sandwell Metropolitan Borough Council", postcode="B68 9TJ"
        )
        == "Sandwell, B68"
    )
    assert customer_safe_location(region="West Midlands", postcode=None) == "West Midlands"


def test_location_omits_missing_or_invalid_postcode() -> None:
    assert customer_safe_location(town="Nottingham", postcode=None) == "Nottingham"
    assert customer_safe_location(town="Nottingham", postcode="unknown") == "Nottingham"


def test_provider_office_fields_are_never_used_as_site_location() -> None:
    title = generated_customer_title(
        {
            "change_type": "OPENING",
            "provider_registered_address": "17 Victoria Road East",
            "provider_registered_postcode": "FY5 5HT",
            "provider_region": "Lancashire",
        }
    )
    assert title == "New children’s home"
    assert "Victoria" not in title
    assert "FY5" not in title
    assert "Lancashire" not in title


def test_apparent_street_address_is_not_accepted_as_a_town() -> None:
    title = generated_customer_title(
        {
            "town": "10 Kingswood Road Nottingham",
            "postcode": "NG8 1LD",
            "change_type": "OPENING",
        }
    )
    assert title == "New children’s home — NG8"
    assert "Kingswood" not in title
    assert "1LD" not in title


@pytest.mark.parametrize(
    ("change_type", "expected"),
    [
        ("OPENING", "New children’s home — Nottingham, NG8"),
        ("EXPANSION", "Children’s home expansion — Nottingham, NG8"),
        ("RELOCATION", "Children’s home relocation — Nottingham, NG8"),
        ("OTHER_CHANGE", "Children’s home development — Nottingham, NG8"),
    ],
)
def test_title_uses_change_type_wording(change_type, expected) -> None:
    assert (
        generated_customer_title(
            {"town": "Nottingham", "postcode": "NG8 1LD", "change_type": change_type}
        )
        == expected
    )


def test_manual_customer_title_override_is_preserved_exactly() -> None:
    assert (
        customer_title(
            {
                "customer_title": "A deliberately chosen customer title",
                "town": "Nottingham",
                "postcode": "NG8 1LD",
                "change_type": "OPENING",
            }
        )
        == "A deliberately chosen customer title"
    )


def test_generated_title_never_contains_street_or_full_postcode() -> None:
    title = generated_customer_title(
        {
            "town": "Nottingham",
            "postcode": "NG8 1LD",
            "address": "10 Kingswood Road",
            "change_type": "OPENING",
        }
    )
    assert "10" not in title
    assert "Kingswood Road" not in title
    assert "NG8 1LD" not in title


def test_customer_evidence_title_suppresses_exact_location() -> None:
    assert safe_evidence_title("Support worker — 3 Rendall Close, L5 4TN") is None
    assert safe_evidence_title("Support worker for a new children’s home in Liverpool") == (
        "Support worker for a new children’s home in Liverpool"
    )


def test_generated_summary_is_stage_aware_without_overstating_certainty() -> None:
    pending = generated_customer_summary(
        {"customer_lifecycle_stage": "PLANNING_PENDING", "source_types": ["planning"]}
    )
    approved = generated_customer_summary(
        {"customer_lifecycle_stage": "PLANNING_APPROVED", "source_types": ["planning"]}
    )
    appeal = generated_customer_summary(
        {"customer_lifecycle_stage": "APPEAL_PENDING", "source_types": ["planning"]}
    )
    assert "awaiting a decision" in pending
    assert "approved" in approved
    assert "appeal is in progress" in appeal
    assert "opening confirmed" not in pending.lower()


def test_generated_summary_does_not_imply_opening_without_foundational_evidence() -> None:
    summary = generated_customer_summary(
        {
            "customer_lifecycle_stage": "NEEDS_REVIEW",
            "source_types": ["planning"],
            "foundational_evidence": False,
        }
    )
    assert "does not establish a new children’s-home opening" in summary
