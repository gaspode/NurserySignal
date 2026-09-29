from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from app.care import enrich_care_signal
from app.care_planning_review import (
    CARE_PLANNING_AI_APPROVAL_POLICY_VERSION,
    CARE_PLANNING_FASTPATH_POLICY_VERSION,
    WITHDRAWAL_POLICY_VERSION,
    care_planning_ai_approval_exclusion,
    care_planning_ai_approval_outcome,
    care_planning_ai_approval_qa_bucket,
    care_planning_ai_approval_qa_holdout,
    care_planning_fastpath_eligible,
    care_planning_fastpath_qa_bucket,
    care_planning_fastpath_qa_holdout,
    classify_care_planning_subtype,
    planning_withdrawal_assessment,
)
from app.config import Settings
from app.repository import (
    apply_care_planning_ai_approval_policy,
    apply_care_planning_fastpath_policy,
    auto_reject_withdrawn_planning,
)


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
        "CONDITION_VARIATION",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
        "FOLLOW_UP_OTHER",
        "AMBIGUOUS",
    ):
        facts = {
            "planning_subtype": subtype,
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
        }
        assert (
            care_planning_ai_approval_exclusion(
                **ai_approval_kwargs(extracted_facts=facts)
            )
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
