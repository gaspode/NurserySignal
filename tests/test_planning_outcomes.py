from app.care import enrich_care_signal
from app.planning_outcomes import PlanningOutcome, canonical_planning_outcome


def raw_planning(
    title: str,
    *,
    planning_status: str | None = None,
    decision: str | None = None,
    external_id: str = "plota:test",
) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000099",
        "vertical": "CHILDRENS_HOME",
        "source_type": "planning",
        "source_url": "https://example.test/planning/test",
        "external_id": external_id,
        "title": title,
        "raw_text": title,
        "location_hint": "Example locality",
        "organisation_hint": None,
        "metadata": {
            "planning_status": planning_status,
            "decision": decision,
            "decision_date": "2026-06-12",
            "provider_record": {
                "id": external_id.removeprefix("plota:"),
                "status": planning_status,
                "decision": {"outcome": decision},
                "description": title,
            },
        },
    }


def test_real_plota_refusal_vocabularies_are_canonical() -> None:
    cases = (
        ("FULL-REF", "Planning Permission - Refused"),
        ("Decided", "Refusal - Full"),
        ("Refused LUC", "Refused LUC"),
        ("Determined", "Refuse"),
        ("Decided", "Certificate Refused (Lawful Dev. Cert.)"),
        ("Final", "Refused; Informatives"),
    )
    for status, decision in cases:
        outcome = canonical_planning_outcome(
            {"planning_status": status, "decision": decision}
        )
        assert outcome.outcome is PlanningOutcome.REFUSED
        assert outcome.refused


def test_refused_proposal_cannot_remain_create_opportunity() -> None:
    candidate = enrich_care_signal(
        raw_planning(
            "Change of use from dwellinghouse to a children's home",
            planning_status="FULL-REF",
            decision="Planning Permission - Refused",
        )
    )
    facts = candidate["extracted_facts"]
    assert facts["planning_outcome"] == "REFUSED"
    assert facts["planning_subtype"] == "REFUSED"
    assert facts["opportunity_creation_decision"] == "IGNORE_FOR_OPPORTUNITY"
    assert candidate["event_type"] == "other"


def test_refusal_under_active_appeal_is_lifecycle_evidence_not_final_cleanup() -> None:
    outcome = canonical_planning_outcome(
        {"planning_status": "Appeal Lodged: REFUSE", "decision": None}
    )
    assert outcome.outcome is PlanningOutcome.REFUSED_UNDER_APPEAL
    assert not outcome.terminal_negative
    candidate = enrich_care_signal(
        raw_planning(
            "Change of use from dwellinghouse to a children's home",
            planning_status="Appeal Lodged: REFUSE",
        )
    )
    facts = candidate["extracted_facts"]
    assert facts["planning_subtype"] == "FOLLOW_UP_OTHER"
    assert facts["opportunity_creation_decision"] == "SUPPORT_EXISTING_ONLY"
    assert candidate["event_type"] == "other"


def test_appeal_lifecycle_outcomes_are_explicit() -> None:
    started = canonical_planning_outcome(
        {
            "planning_status": "Appeal started",
            "decision": "Full Application - Refused Conditions or Reasons: reasons",
        }
    )
    assert started.outcome is PlanningOutcome.REFUSED_UNDER_APPEAL
    assert canonical_planning_outcome(
        {"planning_status": "Appeal allowed"}
    ).outcome is PlanningOutcome.APPEAL_ALLOWED
    dismissed = canonical_planning_outcome({"planning_status": "Appeal dismissed"})
    assert dismissed.outcome is PlanningOutcome.APPEAL_DISMISSED
    assert dismissed.terminal_negative


def test_hillingdon_withdrawn_p_is_not_a_live_opportunity() -> None:
    raw = raw_planning(
        "Proposed change of use to a children's home",
        planning_status="Withdrawn (P)",
        decision="Withdrawn (P)",
        external_id="plota:j78cun1",
    )
    outcome = canonical_planning_outcome(raw["metadata"])
    assert outcome.outcome is PlanningOutcome.WITHDRAWN
    candidate = enrich_care_signal(raw)
    assert candidate["extracted_facts"]["planning_subtype"] == "WITHDRAWN"
    assert candidate["extracted_facts"]["opportunity_creation_decision"] == (
        "IGNORE_FOR_OPPORTUNITY"
    )
    assert candidate["event_type"] == "other"


def test_withdrawal_variants_are_structured_and_appeal_withdrawal_is_not_application_withdrawal(
) -> None:
    for value in (
        "Withdrawn",
        "Withdrawn (P)",
        "Withdrawn - Applicant",
        "Withdrawn after Registration",
        "Withdrawn application",
        "Application Withdrawn",
        "Withdrawn by Applicant",
    ):
        assert canonical_planning_outcome({"decision": value}).outcome is PlanningOutcome.WITHDRAWN
    assert canonical_planning_outcome(
        {"decision": "Appeal Withdrawn by third party"}
    ).outcome is PlanningOutcome.UNKNOWN


def test_positive_pending_and_lawfulness_semantics_survive_outcome_normalisation() -> None:
    for value in (
        "Approved",
        "Grant Conditionally",
        "Planning Permission GRANTED",
        "Application Permitted/Approved",
    ):
        assert canonical_planning_outcome({"decision": value}).outcome is PlanningOutcome.APPROVED
    for value in ("Pending", "Awaiting decision", "Registered: Under Assessment"):
        assert canonical_planning_outcome(
            {"planning_status": value}
        ).outcome is PlanningOutcome.PENDING

    proposed = enrich_care_signal(
        raw_planning(
            "Certificate of Lawfulness (Proposed) for use as a children's home",
            planning_status="Pending",
        )
    )
    existing = enrich_care_signal(
        raw_planning(
            "Certificate of Lawfulness (Existing) for existing use as a children's home",
            planning_status="Approved",
        )
    )
    assert proposed["extracted_facts"]["planning_subtype"] == "LAWFULNESS_PROPOSED"
    assert proposed["extracted_facts"]["opportunity_creation_decision"] == "CREATE_OPPORTUNITY"
    assert existing["extracted_facts"]["planning_subtype"] == "LAWFULNESS_EXISTING"
    assert existing["extracted_facts"]["opportunity_creation_decision"] == (
        "SUPPORT_EXISTING_ONLY"
    )


def test_free_text_refused_word_is_not_an_authoritative_outcome() -> None:
    raw = raw_planning(
        "Resubmission after an earlier application was refused: new children's home",
        planning_status="Pending",
    )
    assert canonical_planning_outcome(raw["metadata"]).outcome is PlanningOutcome.PENDING
    candidate = enrich_care_signal(raw)
    assert candidate["extracted_facts"]["planning_subtype"] != "REFUSED"
