from app.planning_site_identity import extract_planning_site_identity


def planning_raw(**provider):
    return {
        "id": "signal-1",
        "external_id": "plota:REF-1",
        "source_url": "https://example.test/application/REF-1",
        "location_hint": provider.get("address"),
        "metadata": {"council": "Example Council", "provider_record": provider},
    }


def test_extracts_structured_planning_site_identity() -> None:
    identity, outcome = extract_planning_site_identity(
        planning_raw(
            id="REF-1",
            address="1 High Street",
            postcode="OX1 1AA",
            authority={"name": "Example Council"},
            location={"lat": 51.75, "lng": -1.25},
        )
    )
    assert outcome == "FULL_SITE_IDENTITY"
    assert identity["site_address"]["value"] == "1 High Street"
    assert identity["site_postcode"]["value"] == "OX1 1AA"
    assert identity["normalized_site_address"] == "1 high street"


def test_site_identity_does_not_promote_applicant_to_operator() -> None:
    identity, _ = extract_planning_site_identity(
        planning_raw(id="REF-2", address="2 High Street", applicant="Acme Care Ltd")
    )
    assert "applicant" not in identity


def test_postcode_only_is_not_full_site_identity() -> None:
    _, outcome = extract_planning_site_identity(planning_raw(id="REF-3", postcode="OX1 1AA"))
    assert outcome == "POSTCODE_ONLY"
