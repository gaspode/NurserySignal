from app.opportunity_policy import opportunity_creation_decision, opportunity_title


def candidate(source_type, *, raw_text="", facts=None, operator="Little Acorns", address="BS1 1AA"):
    return {
        "source_type": source_type,
        "raw_text": raw_text,
        "operator_name": operator,
        "address": address,
        "metadata": {"postcode": "BS1 1AA"},
        "extracted_facts": {"source_type": source_type, **(facts or {})},
    }


def test_routine_recruitment_supports_existing_only():
    result = opportunity_creation_decision(
        candidate("recruitment", facts={"recruitment_relevance": "RELEVANT_ROUTINE"})
    )
    assert result.decision == "SUPPORT_EXISTING_ONLY"


def test_change_recruitment_creates_opening():
    result = opportunity_creation_decision(
        candidate(
            "recruitment",
            raw_text="Brand new nursery opening soon",
            facts={
                "recruitment_relevance": "RELEVANT_CHANGE",
                "commercial_change_evidence": "STRONG",
            },
        )
    )
    assert result.decision == "CREATE_OPPORTUNITY"
    assert result.change_type == "OPENING"


def test_strong_planning_opening_and_expansion_create():
    opening = opportunity_creation_decision(
        candidate(
            "planning",
            raw_text=(
                "Conversion of existing outbuilding to create nursery pre-school accommodation"
            ),
            facts={"planning_candidate_matched": True},
        )
    )
    expansion = opportunity_creation_decision(
        candidate(
            "planning",
            raw_text=(
                "Retention of increased capacity of existing day nursery "
                "from 18 children to 42 children"
            ),
            facts={"planning_candidate_matched": True},
        )
    )
    assert (opening.decision, opening.change_type) == ("CREATE_OPPORTUNITY", "OPENING")
    assert (expansion.decision, expansion.change_type) == ("CREATE_OPPORTUNITY", "EXPANSION")


def test_relevant_planning_without_material_change_supports_only():
    result = opportunity_creation_decision(
        candidate(
            "planning",
            raw_text="Planning application mentions an existing nursery",
            facts={"planning_candidate_matched": True},
        )
    )
    assert result.decision == "SUPPORT_EXISTING_ONLY"


def test_incidental_nursery_in_wider_development_does_not_create():
    result = opportunity_creation_decision(
        candidate(
            "planning",
            raw_text="New residential development near an existing primary school and nursery",
            facts={"planning_candidate_matched": True},
        )
    )
    assert result.decision == "SUPPORT_EXISTING_ONLY"


def test_irrelevant_signal_is_ignored():
    result = opportunity_creation_decision(
        candidate(
            "planning",
            raw_text="Plant nursery stock storage",
            facts={"likely_false_positive": True},
        )
    )
    assert result.decision == "IGNORE_FOR_OPPORTUNITY"


def test_title_is_concise_and_uses_change_type():
    result = opportunity_creation_decision(
        candidate("planning", raw_text="New nursery", facts={"planning_candidate_matched": True})
    )
    title = opportunity_title(candidate("planning", facts={}), result)
    assert title == "New nursery — Little Acorns (BS1 1AA)"
    assert len(title) < 100
