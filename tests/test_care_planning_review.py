from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4

from app.care import enrich_care_signal
from app.care_planning_review import (
    CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
    CARE_PLANNING_FASTPATH_POLICY_VERSION,
    CARE_PLANNING_LAWFULNESS_POLICY_VERSION,
    WITHDRAWAL_POLICY_VERSION,
    care_planning_ai_approval_exclusion,
    care_planning_ai_approval_outcome,
    care_planning_ai_approval_qa_bucket,
    care_planning_ai_approval_qa_holdout,
    care_planning_fastpath_eligible,
    care_planning_fastpath_qa_bucket,
    care_planning_fastpath_qa_holdout,
    care_planning_lawfulness_exclusion,
    care_planning_lawfulness_outcome,
    care_planning_lawfulness_qa_bucket,
    care_planning_lawfulness_qa_holdout,
    classify_care_planning_subtype,
    planning_withdrawal_assessment,
)
from app.config import Settings
from app.repository import (
    _care_planning_taxonomy_evaluations,
    _taxonomy_v2_reclassified_candidate,
    apply_care_planning_ai_approval_policy,
    apply_care_planning_fastpath_policy,
    apply_care_planning_lawfulness_policy,
    auto_reject_withdrawn_planning,
    care_planning_manual_analysis_from_rows,
)


def test_taxonomy_v2_catchup_selects_only_semantically_reclassified_records() -> None:
    explicit = {
        "extracted_facts": {
            "planning_taxonomy_version": "care-planning-taxonomy-v2",
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
            "planning_classification_history": [
                {"taxonomy_version": "legacy", "planning_subtype": "AMBIGUOUS"}
            ],
        }
    }
    lawfulness = {
        "extracted_facts": {
            "planning_taxonomy_version": "care-planning-taxonomy-v2",
            "planning_subtype": "LAWFULNESS_PROPOSED",
            "planning_classification_history": [
                {"taxonomy_version": "legacy", "planning_subtype": "AMBIGUOUS"}
            ],
        }
    }
    unchanged = {
        "extracted_facts": {
            "planning_taxonomy_version": "care-planning-taxonomy-v2",
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
            "planning_classification_history": [
                {"taxonomy_version": "legacy", "planning_subtype": "NEW_HOME_CHANGE_OF_USE"}
            ],
        }
    }
    assert _taxonomy_v2_reclassified_candidate(explicit) is True
    assert _taxonomy_v2_reclassified_candidate(lawfulness) is True
    assert _taxonomy_v2_reclassified_candidate(unchanged) is False
    assert _taxonomy_v2_reclassified_candidate({"extracted_facts": {}}) is False


def ai_approval_kwargs(**overrides):
    values = {
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "review_status": "PENDING",
        "reviewed_by": None,
        "metadata": {"planning_status": "Pending"},
        "extracted_facts": {
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
        },
        "ai_status": "SUCCEEDED",
        "ai_prompt_version": "care-planning-shadow-v2",
        "ai_recommendation": "APPROVE",
        "ai_confidence": 0.95,
    }
    values.update(overrides)
    return values


def lawfulness_approval_kwargs(**overrides):
    values = {
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "review_status": "PENDING",
        "reviewed_by": None,
        "metadata": {"decision": "Approved"},
        "extracted_facts": {
            "planning_subtype": "LAWFULNESS_PROPOSED",
            "explicit_new_home_proposal": True,
            "opportunity_creation_decision": "CREATE_OPPORTUNITY",
            "planning_prior_references": [],
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
        },
        "ai_status": "SUCCEEDED",
        "ai_prompt_version": "care-planning-shadow-v2",
        "ai_recommendation": "APPROVE",
        "ai_confidence": 0.95,
    }
    values.update(overrides)
    return values


def test_care_ai_approval_v1_exact_eligibility_and_exclusions() -> None:
    assert care_planning_ai_approval_exclusion(**ai_approval_kwargs()) is None
    assert (
        care_planning_ai_approval_exclusion(
            **ai_approval_kwargs(
                extracted_facts={
                    "planning_subtype": "NEW_HOME_OTHER_EXPLICIT",
                    "likely_false_positive": False,
                    "planning_ambiguity_markers": [],
                }
            )
        )
        is None
    )
    for subtype in (
        "LAWFULNESS_PROPOSED",
        "LAWFULNESS_EXISTING",
        "NEW_HOME_MIXED_USE",
        "EXPANSION_OR_CAPACITY_CHANGE",
        "CONDITION_VARIATION",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
        "CESSATION_OR_CHANGE_AWAY_FROM_CARE",
        "AMBIGUOUS",
    ):
        facts = {
            "planning_subtype": subtype,
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
        }
        assert (
            care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
            == "SUBTYPE"
        )
    cases = (
        ({"ai_confidence": 0.90}, "AI_CONFIDENCE"),
        ({"ai_recommendation": "REJECT"}, "AI_RECOMMENDATION"),
        ({"ai_recommendation": "NEEDS_HUMAN"}, "AI_RECOMMENDATION"),
        ({"ai_prompt_version": "care-planning-shadow-v1"}, "AI_VERSION_OR_STATUS"),
        ({"ai_status": "FAILED"}, "AI_VERSION_OR_STATUS"),
        ({"review_status": "APPROVED"}, "EXISTING_REVIEW_OR_POLICY_STATE"),
        ({"reviewed_by": "admin"}, "EXISTING_REVIEW_OR_POLICY_STATE"),
    )
    for overrides, expected in cases:
        assert care_planning_ai_approval_exclusion(**ai_approval_kwargs(**overrides)) == expected


def test_care_ai_approval_v1_outcome_and_false_positive_precedence() -> None:
    signal_id = str(uuid4())
    outcome = care_planning_ai_approval_outcome(signal_id=signal_id, **ai_approval_kwargs())
    expected = "QA_HOLDOUT" if care_planning_ai_approval_qa_holdout(signal_id) else "AUTO_APPROVE"
    assert outcome == expected
    assert care_planning_ai_approval_qa_bucket(signal_id) == care_planning_ai_approval_qa_bucket(
        signal_id
    )
    for decision in ("Refused", "Withdrawn", "Appeal dismissed"):
        assert (
            care_planning_ai_approval_exclusion(
                **ai_approval_kwargs(metadata={"decision": decision})
            )
            == "PLANNING_OUTCOME"
        )
    assert (
        care_planning_ai_approval_exclusion(
            **ai_approval_kwargs(
                metadata={"decision": "Refused", "planning_status": "Appeal started"}
            )
        )
        == "PLANNING_OUTCOME"
    )
    facts = {
        "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
        "likely_false_positive": True,
        "planning_ambiguity_markers": [],
    }
    assert (
        care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
        == "AMBIGUITY_OR_FALSE_POSITIVE"
    )
    facts["likely_false_positive"] = False
    facts["planning_ambiguity_markers"] = ["conflict"]
    assert (
        care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
        == "AMBIGUITY_OR_FALSE_POSITIVE"
    )
    facts["planning_ambiguity_markers"] = []
    facts["care_planning_fastpath"] = {"outcome": "QA_HOLDOUT"}
    assert (
        care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
        == "EXISTING_REVIEW_OR_POLICY_STATE"
    )


def test_care_ai_approval_v1_1_uses_stable_five_percent_future_holdout() -> None:
    signal_ids = [str(UUID(int=value)) for value in range(1, 101)]
    holdouts = [value for value in signal_ids if care_planning_ai_approval_qa_holdout(value)]
    assert len(holdouts) == 5
    assert all(care_planning_ai_approval_qa_bucket(value) == 0 for value in holdouts)
    assert [care_planning_ai_approval_qa_holdout(value) for value in signal_ids] == [
        care_planning_ai_approval_qa_holdout(value) for value in signal_ids
    ]
    # UUID integer 10 belonged to the historical modulo-10 holdout, but is not
    # reassigned by the forward-only modulo-20 v1.1 rule.
    assert UUID(int=10).int % 10 == 0
    assert not care_planning_ai_approval_qa_holdout(str(UUID(int=10)))


def test_historical_care_ai_policy_marker_prevents_reclassification() -> None:
    facts = {
        "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
        "likely_false_positive": False,
        "planning_ambiguity_markers": [],
        "care_planning_ai_approval": {
            "policy_version": "care-planning-ai-approval-v1",
            "outcome": "QA_HOLDOUT",
        },
    }
    assert (
        care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
        == "EXISTING_REVIEW_OR_POLICY_STATE"
    )


def test_proposed_lawfulness_policy_exact_eligibility_and_stable_qa() -> None:
    assert care_planning_lawfulness_exclusion(**lawfulness_approval_kwargs()) is None
    signal_ids = [str(UUID(int=value)) for value in range(1, 101)]
    assert sum(care_planning_lawfulness_qa_holdout(value) for value in signal_ids) == 10
    signal_id = str(UUID(int=20))
    assert care_planning_lawfulness_qa_bucket(signal_id) == 0
    assert (
        care_planning_lawfulness_outcome(signal_id=signal_id, **lawfulness_approval_kwargs())
        == "QA_HOLDOUT"
    )
    assert care_planning_lawfulness_qa_holdout(signal_id)


def test_proposed_lawfulness_policy_excludes_unvalidated_semantics() -> None:
    base_facts = lawfulness_approval_kwargs()["extracted_facts"]
    cases = (
        ({"extracted_facts": {**base_facts, "planning_subtype": "LAWFULNESS_EXISTING"}}, "SUBTYPE"),
        ({"ai_confidence": 0.90}, "AI_CONFIDENCE"),
        ({"ai_recommendation": "REJECT"}, "AI_RECOMMENDATION"),
        ({"ai_recommendation": "NEEDS_HUMAN"}, "AI_RECOMMENDATION"),
        ({"ai_status": "FAILED"}, "AI_VERSION_OR_STATUS"),
        ({"ai_prompt_version": "care-planning-shadow-v1"}, "AI_VERSION_OR_STATUS"),
        ({"metadata": {"decision": "Refused"}}, "PLANNING_OUTCOME"),
        ({"metadata": {"decision": "Withdrawn"}}, "PLANNING_OUTCOME"),
        (
            {"metadata": {"decision": "Refused", "planning_status": "Appeal started"}},
            "PLANNING_OUTCOME",
        ),
        ({"metadata": {"decision": "Appeal dismissed"}}, "PLANNING_OUTCOME"),
        (
            {
                "extracted_facts": {
                    **base_facts,
                    "opportunity_creation_decision": "SUPPORT_EXISTING_ONLY",
                }
            },
            "OPPORTUNITY_DECISION",
        ),
        (
            {"extracted_facts": {**base_facts, "opportunity_creation_decision": "REVIEW"}},
            "OPPORTUNITY_DECISION",
        ),
        (
            {"extracted_facts": {**base_facts, "likely_false_positive": True}},
            "LIKELY_FALSE_POSITIVE",
        ),
        (
            {"extracted_facts": {**base_facts, "planning_ambiguity_markers": ["unclear"]}},
            "AMBIGUITY",
        ),
        (
            {"extracted_facts": {**base_facts, "planning_prior_references": ["24/1234/F"]}},
            "PRIOR_APPLICATION_REFERENCE",
        ),
        ({"review_status": "APPROVED"}, "EXISTING_REVIEW_OR_POLICY_STATE"),
        ({"reviewed_by": "admin"}, "EXISTING_REVIEW_OR_POLICY_STATE"),
        (
            {
                "extracted_facts": {
                    **base_facts,
                    "care_planning_ai_approval": {
                        "policy_version": "care-planning-ai-approval-v1.1"
                    },
                }
            },
            "EXISTING_REVIEW_OR_POLICY_STATE",
        ),
    )
    for overrides, expected in cases:
        assert (
            care_planning_lawfulness_exclusion(**lawfulness_approval_kwargs(**overrides))
            == expected
        )


def raw_planning(
    text: str,
    *,
    decision: str = "Pending",
    signal_id: str = "00000000-0000-0000-0000-000000000004",
) -> dict:
    return {
        "id": signal_id,
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "source_url": "https://example.test/planning/CARE-1",
        "external_id": "plota:CARE-1",
        "title": text,
        "raw_text": text,
        "location_hint": "14 Example Road, Sheffield",
        "organisation_hint": "Example Applicant Ltd",
        "metadata": {
            "postcode": "S1 1AA",
            "decision": decision,
            "provider_record": {
                "id": "CARE-1",
                "description": text,
                "address": "14 Example Road, Sheffield",
                "postcode": "S1 1AA",
                "status": decision,
                "decision": {"outcome": decision},
            },
        },
    }


def test_explicit_change_of_use_and_other_new_home_subtypes() -> None:
    change = classify_care_planning_subtype(
        raw_planning("Use of dwellinghouse (C3) as a children's care home (C2)")
    )
    other = classify_care_planning_subtype(
        raw_planning("Proposed residential children's home for three young people")
    )
    assert change.subtype == "NEW_HOME_CHANGE_OF_USE"
    assert change.explicit_new_home is True
    assert other.subtype == "NEW_HOME_OTHER_EXPLICIT"


def test_direction_aware_new_home_variants() -> None:
    proposals = (
        "Proposed change of use from dwelling (Class C3) to a residential home "
        "for up to two children (Class C2)",
        "Change of use from a C4 HMO to a C2 residential children's home",
        "Conversion of former educational PRU building to a residential care home "
        "for four children (C2)",
        "Conversion of dwellinghouse to a children's care home",
        "Change of use from C3 residential to Class C2 residential care home for young people",
    )
    for proposal in proposals:
        candidate = enrich_care_signal(raw_planning(proposal))
        assert candidate["extracted_facts"]["planning_subtype"] == "NEW_HOME_CHANGE_OF_USE"
        assert candidate["extracted_facts"]["opportunity_creation_decision"] == (
            "CREATE_OPPORTUNITY"
        )


def test_mixed_use_new_home_is_separate_and_not_in_existing_approval_scope() -> None:
    candidate = enrich_care_signal(
        raw_planning(
            "Outline application for a mixed-use development comprising 256 dwellings, "
            "housing with care, a new children's home and associated works"
        )
    )
    facts = candidate["extracted_facts"]
    assert facts["planning_subtype"] == "NEW_HOME_MIXED_USE"
    assert facts["opportunity_creation_decision"] == "CREATE_OPPORTUNITY"
    assert not care_planning_fastpath_eligible(
        vertical="CHILDRENS_HOME",
        source_type="planning",
        review_status="PENDING",
        extracted_facts=facts,
    )
    assert (
        care_planning_ai_approval_exclusion(**ai_approval_kwargs(extracted_facts=facts))
        == "SUBTYPE"
    )

    incidental = classify_care_planning_subtype(
        raw_planning("Outline health campus with an adult care home and a children's day nursery")
    )
    assert incidental.subtype == "AMBIGUOUS"


def test_change_away_from_children_care_is_not_an_opening() -> None:
    for proposal in (
        "Change of use from children's care home (C2) to single dwelling (C3)",
        "Conversion from Class C2 children's home to Class E offices",
        "Cessation of children's home use and return to a dwellinghouse",
    ):
        candidate = enrich_care_signal(raw_planning(proposal))
        facts = candidate["extracted_facts"]
        assert facts["planning_subtype"] == "CESSATION_OR_CHANGE_AWAY_FROM_CARE"
        assert facts["opportunity_creation_decision"] == "IGNORE_FOR_OPPORTUNITY"


def test_existing_home_capacity_change_is_expansion_not_opening() -> None:
    for proposal in (
        "Continued use as a children's care home with the addition of one young person",
        "Extension to the existing children's home increasing capacity from 3 children to 4",
    ):
        candidate = enrich_care_signal(raw_planning(proposal))
        facts = candidate["extracted_facts"]
        assert facts["planning_subtype"] == "EXPANSION_OR_CAPACITY_CHANGE"
        assert facts["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"
        assert facts["opportunity_change_type"] == "EXPANSION"


def test_granted_and_pending_explicit_home_remain_fastpath_candidates() -> None:
    for decision in ("Grant Conditionally", "Pending Consideration"):
        candidate = enrich_care_signal(
            raw_planning(
                "Change of use from dwellinghouse to children's care home", decision=decision
            )
        )
        facts = candidate["extracted_facts"]
        assert facts["planning_subtype"] == "NEW_HOME_CHANGE_OF_USE"
        assert facts["opportunity_creation_decision"] == "CREATE_OPPORTUNITY"
        assert care_planning_fastpath_eligible(
            vertical="CHILDRENS_HOME",
            source_type="planning",
            review_status="PENDING",
            extracted_facts=facts,
        )


def test_lawfulness_proposed_existing_and_ambiguous_are_distinct() -> None:
    proposed = enrich_care_signal(
        raw_planning("Certificate of Lawfulness (Proposed) for use as a children's home")
    )
    existing = enrich_care_signal(
        raw_planning("Certificate of Lawfulness (Existing) for existing use as a children's home")
    )
    ambiguous = enrich_care_signal(raw_planning("Certificate of Lawfulness for a children's home"))
    assert proposed["extracted_facts"]["planning_subtype"] == "LAWFULNESS_PROPOSED"
    assert proposed["extracted_facts"]["opportunity_creation_decision"] == "CREATE_OPPORTUNITY"
    assert existing["extracted_facts"]["planning_subtype"] == "LAWFULNESS_EXISTING"
    assert existing["extracted_facts"]["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"
    assert ambiguous["extracted_facts"]["planning_subtype"] == "AMBIGUOUS"
    assert ambiguous["extracted_facts"]["opportunity_creation_decision"] == "REVIEW"


def test_lawfulness_terminology_variants_follow_proposed_existing_direction() -> None:
    proposed = (
        "Certificate of lawful development for proposed change of use from dwellinghouse "
        "to a residential care home for two children",
        "Application for a Lawful Development Certificate under Section 192 for use of "
        "a C3 dwelling as a children's home (C2)",
        "Certificate of Lawfulness for change of use from C3 to a C2 residential children's home",
    )
    for proposal in proposed:
        assert (
            enrich_care_signal(raw_planning(proposal))["extracted_facts"]["planning_subtype"]
            == "LAWFULNESS_PROPOSED"
        )
    existing = enrich_care_signal(
        raw_planning("Certificate of lawfulness for existing use as a children's home")
    )
    assert existing["extracted_facts"]["planning_subtype"] == "LAWFULNESS_EXISTING"
    assert existing["extracted_facts"]["opportunity_creation_decision"] == ("SUPPORT_EXISTING_ONLY")


def test_follow_ups_do_not_create_duplicate_openings_and_retain_reference() -> None:
    cases = {
        "Variation of condition 2 of planning application 24/01234/FUL for a children's home": (
            "CONDITION_VARIATION",
            ["24/01234/FUL"],
        ),
        "Discharge of conditions 3 and 4 for a children's home": (
            "CONDITION_DISCHARGE",
            [],
        ),
        "Non-material amendment to children's home permission": (
            "NON_MATERIAL_AMENDMENT",
            [],
        ),
    }
    for text, (subtype, references) in cases.items():
        candidate = enrich_care_signal(raw_planning(text))
        facts = candidate["extracted_facts"]
        assert facts["planning_subtype"] == subtype
        assert facts["opportunity_creation_decision"] != "CREATE_OPPORTUNITY"
        assert facts["planning_prior_references"] == references


def test_current_follow_up_purpose_beats_quoted_original_permission() -> None:
    variation = enrich_care_signal(
        raw_planning(
            "Variation of condition 4 of permission 24/1234/FUL for change of use from "
            "C3 dwelling to C2 children's home, to alter staff numbers"
        )
    )
    discharge = enrich_care_signal(
        raw_planning(
            "Submission of details to discharge condition 3 imposed on permission "
            "24/5678/FUL for change of use from C3 dwelling to C2 children's home"
        )
    )
    assert variation["extracted_facts"]["planning_subtype"] == "CONDITION_VARIATION"
    assert discharge["extracted_facts"]["planning_subtype"] == "CONDITION_DISCHARGE"
    assert variation["extracted_facts"]["opportunity_creation_decision"] == (
        "SUPPORT_EXISTING_ONLY"
    )
    assert discharge["extracted_facts"]["opportunity_creation_decision"] == (
        "SUPPORT_EXISTING_ONLY"
    )


def test_taxonomy_preview_evaluation_does_not_mutate_review_or_ai_state() -> None:
    signal_id = uuid4()
    item = {
        **raw_planning(
            "Proposed change of use from dwelling (C3) to a residential care home "
            "for two children (C2)",
            signal_id=str(signal_id),
            decision="Approved",
        ),
        "id": signal_id,
        "review_status": "PENDING",
        "reviewed_by": None,
        "existing_facts": {
            "planning_subtype": "AMBIGUOUS",
            "opportunity_creation_decision": "REVIEW",
            "opportunity_change_type": "OPENING",
            "planning_ambiguity_markers": ["insufficient planning semantics"],
        },
        "ai_status": "SUCCEEDED",
        "ai_recommendation": "APPROVE",
        "ai_confidence": 0.95,
        "ai_prompt_version": "care-planning-shadow-v2",
    }
    evaluation = _care_planning_taxonomy_evaluations([item])[0]
    assert evaluation["old_subtype"] == "AMBIGUOUS"
    assert evaluation["new_subtype"] == "NEW_HOME_CHANGE_OF_USE"
    assert evaluation["changed"] is True
    assert item["review_status"] == "PENDING"
    assert item["ai_recommendation"] == "APPROVE"
    assert item["existing_facts"]["planning_subtype"] == "AMBIGUOUS"


def test_prior_reference_extraction_rejects_prose_after_application() -> None:
    candidate = enrich_care_signal(
        raw_planning(
            "Application submitted under Section 73 for a minor amendment to planning "
            "permission 25/2371/F for a children's home"
        )
    )
    assert candidate["extracted_facts"]["planning_prior_references"] == ["25/2371/F"]


def test_material_capacity_variation_is_relevant_but_support_only() -> None:
    candidate = enrich_care_signal(
        raw_planning(
            "Variation of condition to increase occupancy of existing children's home "
            "from 3 to 5 places"
        )
    )
    facts = candidate["extracted_facts"]
    assert facts["planning_subtype"] == "CONDITION_VARIATION"
    assert facts["planning_material_capacity_change"] is True
    assert facts["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"


def test_withdrawal_policy_is_exact_and_does_not_blacklist_resubmission() -> None:
    for value in ("Withdrawn", "Application Withdrawn", "Withdrawn by Applicant"):
        assert planning_withdrawal_assessment({"decision": value}).withdrawn
    for value in ("Invalid", "Returned", "Appeal Withdrawn by third party"):
        assert not planning_withdrawal_assessment({"decision": value}).withdrawn
    resubmission = raw_planning("Proposed change of use to a children's home", decision="Pending")
    assert classify_care_planning_subtype(resubmission).subtype == "NEW_HOME_CHANGE_OF_USE"


def test_fastpath_holdout_is_stable_and_lawfulness_is_not_in_v1() -> None:
    signal_id = str(uuid4())
    assert care_planning_fastpath_qa_bucket(signal_id) == care_planning_fastpath_qa_bucket(
        signal_id
    )
    assert care_planning_fastpath_qa_holdout(signal_id) == (
        care_planning_fastpath_qa_bucket(signal_id) == 0
    )
    lawfulness = enrich_care_signal(
        raw_planning("Certificate of Lawfulness (Proposed) for use as a children's home")
    )
    assert not care_planning_fastpath_eligible(
        vertical="CHILDRENS_HOME",
        source_type="planning",
        review_status="PENDING",
        extracted_facts=lawfulness["extracted_facts"],
    )


def test_invalid_or_ambiguous_decision_cannot_enter_fastpath() -> None:
    for decision in ("Invalid", "Returned", "Appeal Pending"):
        candidate = enrich_care_signal(
            raw_planning(
                "Change of use from dwellinghouse to children's care home",
                decision=decision,
            )
        )
        facts = candidate["extracted_facts"]
        assert facts["planning_subtype"] == "NEW_HOME_CHANGE_OF_USE"
        assert facts["planning_decision_fastpath_eligible"] is False
        assert not care_planning_fastpath_eligible(
            vertical="CHILDRENS_HOME",
            source_type="planning",
            review_status="PENDING",
            extracted_facts=facts,
        )


class PlanningPolicyConnection:
    def __init__(
        self, signal_id: str, *, withdrawal: bool = False, decision: str = "Pending"
    ) -> None:
        self.signal_id = signal_id
        self.status = "PENDING"
        self.withdrawal = withdrawal
        self.decision = decision
        self.statements: list[tuple[str, object]] = []
        self.last_result: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT rs.id, rs.vertical" in sql:
            self.last_result = [
                (
                    self.signal_id,
                    "CHILDRENS_HOME",
                    "planning",
                    {"decision": self.decision},
                    self.status,
                    {
                        "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
                        "planning_candidate_matched": True,
                        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                        "explicit_new_home_proposal": True,
                        "planning_decision_fastpath_eligible": True,
                        "likely_false_positive": False,
                        "planning_ambiguity_markers": [],
                        "planning_subtype_reasons": ["explicit change of use"],
                    },
                )
            ]
        elif "UPDATE signal_enrichments" in sql and self.status == "PENDING":
            self.status = "APPROVED" if "review_status = 'APPROVED'" in sql else "PENDING"
            self.last_result = [("CHILDRENS_HOME",) if self.withdrawal else (self.signal_id,)]
        else:
            self.last_result = []
        return self

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self) -> None:
        return None


def test_fastpath_auto_approval_is_audited_and_idempotent(monkeypatch) -> None:
    signal_id = str(uuid4())
    while care_planning_fastpath_qa_holdout(signal_id):
        signal_id = str(uuid4())
    fake = PlanningPolicyConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    first = apply_care_planning_fastpath_policy(Settings(), signal_id)
    second = apply_care_planning_fastpath_policy(Settings(), signal_id)
    assert first["outcome"] == "AUTO_APPROVE" and first["updated"] is True
    assert second["updated"] is False
    audits = [sql for sql, _ in fake.statements if "INSERT INTO admin_audit_events" in sql]
    assert len(audits) == 1
    update = next(params for sql, params in fake.statements if "review_status = 'APPROVED'" in sql)
    assert update[0] == f"system:{CARE_PLANNING_FASTPATH_POLICY_VERSION}"


def test_refusal_precedes_care_fastpath(monkeypatch) -> None:
    signal_id = str(uuid4())
    fake = PlanningPolicyConnection(signal_id, decision="Refusal")

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = apply_care_planning_fastpath_policy(Settings(), signal_id)
    assert result == {"signal_id": signal_id, "outcome": "INELIGIBLE", "updated": False}
    assert not any("CARE_PLANNING_FASTPATH" in sql for sql, _ in fake.statements)


def test_withdrawn_auto_rejection_uses_separate_versioned_audit(monkeypatch) -> None:
    signal_id = str(uuid4())
    fake = PlanningPolicyConnection(signal_id, withdrawal=True)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    assert auto_reject_withdrawn_planning(
        Settings(), signal_id, {"decision": "Application Withdrawn"}
    )
    audit = next(
        params for sql, params in fake.statements if "PLANNING_WITHDRAWAL_AUTO_REJECT" in sql
    )
    assert audit[0] == f"system:{WITHDRAWAL_POLICY_VERSION}"
    assert audit[1].obj["policy_version"] == WITHDRAWAL_POLICY_VERSION


class CareAiApprovalConnection:
    def __init__(self, signal_id: str) -> None:
        self.signal_id = signal_id
        self.status = "PENDING"
        self.reviewed_by = None
        self.marker = None
        self.statements: list[tuple[str, object]] = []
        self.last_result: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT rs.id, rs.vertical" in sql:
            facts = {
                "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
                "likely_false_positive": False,
                "planning_ambiguity_markers": [],
            }
            if self.marker:
                facts["care_planning_ai_approval"] = self.marker
            self.last_result = [
                (
                    self.signal_id,
                    "CHILDRENS_HOME",
                    "planning",
                    {"planning_status": "Pending"},
                    self.status,
                    self.reviewed_by,
                    facts,
                    "SUCCEEDED",
                    "APPROVE",
                    0.95,
                    "care-planning-shadow-v2",
                )
            ]
        elif "UPDATE signal_enrichments" in sql and self.status == "PENDING":
            self.marker = params[1 if "review_status = 'APPROVED'" in sql else 0].obj[
                "care_planning_ai_approval"
            ]
            if "review_status = 'APPROVED'" in sql:
                self.status = "APPROVED"
                self.reviewed_by = params[0]
            self.last_result = [(self.signal_id,)]
        else:
            self.last_result = []
        return self

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self) -> None:
        return None


def test_care_ai_auto_approval_is_audited_and_idempotent(monkeypatch) -> None:
    signal_id = str(uuid4())
    while care_planning_ai_approval_qa_holdout(signal_id):
        signal_id = str(uuid4())
    fake = CareAiApprovalConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    first = apply_care_planning_ai_approval_policy(Settings(), signal_id)
    second = apply_care_planning_ai_approval_policy(Settings(), signal_id)
    assert first == {"signal_id": signal_id, "outcome": "AUTO_APPROVE", "updated": True}
    assert second["updated"] is False
    assert fake.reviewed_by == f"system:{CARE_PLANNING_AI_APPROVAL_POLICY_VERSION}"
    assert fake.marker["ai_prompt_version"] == "care-planning-shadow-v2"
    assert fake.marker["ai_confidence"] == 0.95
    audits = [sql for sql, _ in fake.statements if "INSERT INTO admin_audit_events" in sql]
    assert len(audits) == 1


def test_care_ai_qa_holdout_remains_pending(monkeypatch) -> None:
    signal_id = str(uuid4())
    while not care_planning_ai_approval_qa_holdout(signal_id):
        signal_id = str(uuid4())
    fake = CareAiApprovalConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = apply_care_planning_ai_approval_policy(Settings(), signal_id)
    assert result == {"signal_id": signal_id, "outcome": "QA_HOLDOUT", "updated": True}
    assert fake.status == "PENDING"
    assert fake.marker["qa_bucket"] == 0


class LawfulnessApprovalConnection:
    def __init__(self, signal_id: str) -> None:
        self.signal_id = signal_id
        self.status = "PENDING"
        self.reviewed_by = None
        self.marker = None
        self.statements: list[tuple[str, object]] = []
        self.last_result: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT rs.id, rs.vertical" in sql:
            facts = {
                "planning_subtype": "LAWFULNESS_PROPOSED",
                "explicit_new_home_proposal": True,
                "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                "planning_prior_references": [],
                "likely_false_positive": False,
                "planning_ambiguity_markers": [],
            }
            if self.marker:
                facts["care_planning_lawfulness_approval"] = self.marker
            self.last_result = [
                (
                    self.signal_id,
                    "CHILDRENS_HOME",
                    "planning",
                    {"decision": "Approved"},
                    self.status,
                    self.reviewed_by,
                    facts,
                    "SUCCEEDED",
                    "APPROVE",
                    0.95,
                    "care-planning-shadow-v2",
                )
            ]
        elif "UPDATE signal_enrichments" in sql and self.status == "PENDING":
            marker_index = 1 if "review_status = 'APPROVED'" in sql else 0
            self.marker = params[marker_index].obj["care_planning_lawfulness_approval"]
            if "review_status = 'APPROVED'" in sql:
                self.status = "APPROVED"
                self.reviewed_by = params[0]
            self.last_result = [(self.signal_id,)]
        else:
            self.last_result = []
        return self

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self) -> None:
        return None


def test_proposed_lawfulness_auto_approval_is_audited_and_idempotent(monkeypatch) -> None:
    signal_id = str(uuid4())
    while care_planning_lawfulness_qa_holdout(signal_id):
        signal_id = str(uuid4())
    fake = LawfulnessApprovalConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    first = apply_care_planning_lawfulness_policy(Settings(), signal_id)
    second = apply_care_planning_lawfulness_policy(Settings(), signal_id)
    assert first == {"signal_id": signal_id, "outcome": "AUTO_APPROVE", "updated": True}
    assert second["updated"] is False
    assert fake.reviewed_by == f"system:{CARE_PLANNING_LAWFULNESS_POLICY_VERSION}"
    assert fake.marker["planning_subtype"] == "LAWFULNESS_PROPOSED"
    assert fake.marker["planning_outcome"] == "APPROVED"
    assert fake.marker["opportunity_creation_decision"] == "CREATE_OPPORTUNITY"
    audits = [sql for sql, _ in fake.statements if "INSERT INTO admin_audit_events" in sql]
    assert len(audits) == 1
    assert not any("publication_status" in sql for sql, _ in fake.statements)


def test_proposed_lawfulness_qa_holdout_remains_pending(monkeypatch) -> None:
    signal_id = str(UUID(int=20))
    fake = LawfulnessApprovalConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    result = apply_care_planning_lawfulness_policy(Settings(), signal_id)
    assert result == {"signal_id": signal_id, "outcome": "QA_HOLDOUT", "updated": True}
    assert fake.status == "PENDING"
    assert fake.marker["qa_bucket"] == 0


def test_manual_cohort_analysis_and_lawfulness_preview_are_read_only() -> None:
    def row(signal_int: int, subtype: str, recommendation: str, confidence: float) -> dict:
        return {
            "id": UUID(int=signal_int),
            "vertical": "CHILDRENS_HOME",
            "source_type": "planning",
            "review_status": "PENDING",
            "reviewed_by": None,
            "metadata": {"planning_status": "Pending"},
            "extracted_facts": {
                "planning_subtype": subtype,
                "explicit_new_home_proposal": True,
                "likely_false_positive": False,
                "planning_ambiguity_markers": [],
                "planning_candidate_matched": True,
                "planning_prior_references": [],
                "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                "opportunity_change_type": "OPENING",
            },
            "ai_status": "SUCCEEDED",
            "ai_recommendation": recommendation,
            "ai_confidence": confidence,
            "ai_prompt_version": "care-planning-shadow-v2",
        }

    rows = [
        row(20, "LAWFULNESS_PROPOSED", "APPROVE", 0.95),
        row(21, "LAWFULNESS_PROPOSED", "REJECT", 0.95),
        row(22, "CONDITION_DISCHARGE", "REJECT", 0.95),
    ]
    report = care_planning_manual_analysis_from_rows(rows)
    lawfulness = report["lawfulness_proposed"]
    preview = report["lawfulness_policy_preview"]
    assert report["pending_total"] == 3
    assert lawfulness["total_pending"] == 2
    assert lawfulness["ai_recommendations"] == {"APPROVE": 1, "REJECT": 1}
    assert preview["eligible"] == 1
    assert preview["qa_holdouts_at_10_percent"] == 1
    assert preview["would_auto_approve"] == 0
    assert preview["excluded_reasons"] == {"AI_RECOMMENDATION": 1}
    assert report["no_new_subtype_automation_enabled"] is True
