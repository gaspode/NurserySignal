from app.planning_families import (
    PlanningFamilyIdentity,
    followup_can_support_opportunity,
    is_foundational_planning_signal,
    normalize_planning_reference,
    prior_planning_references,
)


def test_reference_normalization_preserves_semantics() -> None:
    assert normalize_planning_reference("24 / 03385 / ful") == "24/03385/FUL"
    assert normalize_planning_reference(" 2025-0194 ") == "2025-0194"
    assert normalize_planning_reference("condition three") is None


def test_family_identity_is_scoped_by_authority() -> None:
    croydon = PlanningFamilyIdentity.create("London Borough of Croydon", "24/03385/FUL")
    leeds = PlanningFamilyIdentity.create("Leeds City Council", "24/03385/FUL")
    spaced = PlanningFamilyIdentity.create("London Borough of Croydon", "24 / 03385 / ful")
    assert croydon is not None and leeds is not None and spaced is not None
    assert croydon.normalized_reference == spaced.normalized_reference
    assert croydon.normalized_authority == spaced.normalized_authority
    assert croydon.normalized_authority != leeds.normalized_authority


def test_prior_reference_extraction_rejects_weak_numeric_text() -> None:
    raw = {
        "title": "Details pursuant to permission ref. 24 / 03385 / FUL",
        "raw_text": "Condition 3 under Section 73",
    }
    assert prior_planning_references(raw, {}) == (
        ("24 / 03385 / FUL", "24/03385/FUL"),
    )
    assert prior_planning_references(
        {"title": "Condition 3 under Section 73", "raw_text": ""}, {}
    ) == ()


def test_foundational_and_support_only_semantics_do_not_inherit_review_state() -> None:
    pending = {"planning_status": "Pending Consideration"}
    refused = {"decision": "Planning Permission - Refused"}
    assert is_foundational_planning_signal("NEW_HOME_CHANGE_OF_USE", pending)
    assert not is_foundational_planning_signal("NEW_HOME_CHANGE_OF_USE", refused)
    assert not is_foundational_planning_signal("LAWFULNESS_EXISTING", pending)
    assert followup_can_support_opportunity("CONDITION_DISCHARGE", pending)
    assert not followup_can_support_opportunity("CONDITION_DISCHARGE", refused)
