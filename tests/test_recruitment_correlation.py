from app.recruitment_correlation import classify_recruitment_match


def signal(**overrides):
    value = {
        "organisation_hint": "Acorn Childcare Ltd",
        "location_hint": "12 High Street",
        "metadata": {"postcode": "AB1 2CD", "town": "Exampleton"},
        "extracted_facts": {},
    }
    value.update(overrides)
    return value


def opportunity(**overrides):
    value = {
        "name": "Acorn Nursery — Exampleton",
        "operator_name": "Acorn Childcare Ltd",
        "address": "12 High Street",
        "postcode": "AB1 2CD",
        "town": "Exampleton",
    }
    value.update(overrides)
    return value


def test_exact_operator_and_site_is_exact() -> None:
    result = classify_recruitment_match(signal(), opportunity())
    assert result.outcome == "EXACT"
    assert "organisation_exact" in result.reason_codes
    assert "address_exact" in result.reason_codes


def test_exact_operator_and_postcode_is_strong() -> None:
    result = classify_recruitment_match(
        signal(location_hint="Exampleton"), opportunity(address="12 High Street")
    )
    assert result.outcome == "STRONG"


def test_postcode_only_is_not_an_automatic_match() -> None:
    result = classify_recruitment_match(
        signal(organisation_hint="", location_hint=""), opportunity(operator_name="")
    )
    assert result.outcome == "UNCERTAIN"
    assert "postcode_without_corroboration" in result.reason_codes


def test_same_postcode_conflicting_operator_is_rejected() -> None:
    result = classify_recruitment_match(signal(), opportunity(operator_name="Different Care Ltd"))
    assert result.outcome == "NO_MATCH"
    assert result.reason_codes == ("conflicting_operator",)


def test_same_operator_different_site_is_not_auto_linked() -> None:
    result = classify_recruitment_match(
        signal(location_hint="Other Town", metadata={"postcode": "ZZ1 1ZZ", "town": "Other"}),
        opportunity(),
    )
    assert result.outcome == "NO_MATCH"


def test_ambiguous_site_is_retained_for_review() -> None:
    result = classify_recruitment_match(
        signal(
            organisation_hint="Acorn Childcare Ltd",
            location_hint="",
            metadata={"town": "Exampleton"},
        ),
        opportunity(postcode="", address=""),
    )
    assert result.outcome == "UNCERTAIN"
    assert "site_not_identified" in result.reason_codes
