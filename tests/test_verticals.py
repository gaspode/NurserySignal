import pytest
from app.ingestion import NormalizedSignal
from app.verticals import (
    ALL_VERTICALS,
    CHILDRENS_HOME,
    DENTAL,
    NURSERY,
    VERTICAL_REGISTRY,
    registry_payload,
    validate_vertical,
)


def test_registry_contains_enabled_nursery_and_care_verticals():
    registry = {item["key"]: item for item in registry_payload()}
    assert registry[NURSERY]["enabled"] is True
    assert registry[CHILDRENS_HOME]["enabled"] is True
    assert registry[DENTAL]["enabled"] is False
    assert VERTICAL_REGISTRY[NURSERY].recruitment_routes == ("Education and early years",)
    assert VERTICAL_REGISTRY[CHILDRENS_HOME].recruitment_routes == ("Care services",)


def test_all_is_only_valid_for_admin_filters():
    assert validate_vertical(ALL_VERTICALS, allow_all=True) == ALL_VERTICALS
    with pytest.raises(ValueError):
        validate_vertical(ALL_VERTICALS)


def test_normalized_signal_defaults_to_nursery_vertical():
    signal = NormalizedSignal.from_dict(
        {
            "source_type": "planning",
            "source_url": "https://example.test/application/1",
            "external_id": "1",
            "discovered_at": "2026-09-26T00:00:00Z",
            "title": "New nursery",
            "raw_text": "New nursery provision",
        }
    )
    assert signal.vertical == NURSERY


def test_care_vertical_can_be_ingested_as_active():
    signal = NormalizedSignal.from_dict(
        {
            "vertical": CHILDRENS_HOME,
            "source_type": "planning",
            "source_url": "https://example.test/application/1",
            "external_id": "1",
            "discovered_at": "2026-09-26T00:00:00Z",
            "title": "Children's home",
            "raw_text": "Children's home",
        }
    )
    assert signal.vertical == CHILDRENS_HOME


def test_disabled_dental_vertical_cannot_be_ingested_as_active():
    with pytest.raises(ValueError, match="not enabled"):
        NormalizedSignal.from_dict(
            {
                "vertical": DENTAL,
                "source_type": "planning",
                "source_url": "https://example.test/application/1",
                "external_id": "1",
                "discovered_at": "2026-09-26T00:00:00Z",
                "title": "Care home",
                "raw_text": "Care home",
            }
        )
