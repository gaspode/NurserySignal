from contextlib import contextmanager
from uuid import uuid4

from app.config import Settings
from app.repository import (
    apply_safe_approval_policy,
    auto_reject_refused_planning,
    pending_review_triage_ids,
)
from app.review_triage import (
    REFUSAL_POLICY_VERSION,
    deterministic_review_recommendation,
    normalize_planning_decision,
    planning_refusal_assessment,
    review_triage_bucket,
    safe_approval_candidate,
    safe_approval_policy_outcome,
    safe_approval_qa_bucket,
    safe_approval_qa_holdout,
)


def test_structured_refusal_variants_are_unambiguous() -> None:
    for value in (
        "REFUSED",
        "Refusal",
        "refusal.",
        "rejected",
        "Permission: refused",
        "Application refused.",
        "Refuse Permission/Consent",
        "Refuse Permission",
        "Refuse Consent",
        "Refusal of Permission",
        "Refusal of Consent",
        "Permission/Consent Refused",
        "Certificate of Lawfulness — Refused",
    ):
        result = planning_refusal_assessment(
            {"planning_status": value, "decision_date": "2026-04-03"}
        )
        assert result.refused is True
        assert result.decision_date == "2026-04-03"
    assert normalize_planning_decision("Permission: refused.") == "PERMISSION REFUSED"
    assert REFUSAL_POLICY_VERSION == "planning-refusal-v4"


def test_non_refusal_terminal_states_are_not_auto_rejected() -> None:
    for value in (
        "WITHDRAWN",
        "INVALID",
        "RETURNED",
        "LAPSED",
        "EXPIRED",
        "Conditions discharged",
        "Non-material amendment",
        "Appeal pending",
        "Appeal allowed",
    ):
        assert planning_refusal_assessment({"decision": value}).refused is False
    assert planning_refusal_assessment({"decision": "Appeal dismissed"}).refused is True


def test_nested_provider_decision_is_used_but_description_text_is_not() -> None:
    assert planning_refusal_assessment(
        {"provider_record": {"decision": {"outcome": "Permission refused"}}}
    ).refused
    assert not planning_refusal_assessment(
        {"description": "Previous permission refused; this is a revised application"}
    ).refused


def test_later_resubmission_is_independently_processable() -> None:
    refused = {"reference": "A/1", "decision": "Refused"}
    resubmission = {"reference": "A/2", "planning_status": "Pending"}
    assert planning_refusal_assessment(refused).refused
    assert not planning_refusal_assessment(resubmission).refused


def test_safe_agreement_requires_deterministic_approve_and_high_confidence_ai() -> None:
    facts = {
        "planning_candidate_matched": True,
        "likely_false_positive": False,
        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
    }
    assert deterministic_review_recommendation(facts) == "APPROVE"
    assert safe_approval_candidate(
        source_type="planning",
        review_status="PENDING",
        metadata={"planning_status": "Pending"},
        extracted_facts=facts,
        ai_status="SUCCEEDED",
        ai_recommendation="APPROVE",
        ai_confidence=0.95,
    )
    assert not safe_approval_candidate(
        source_type="planning",
        review_status="PENDING",
        metadata={"decision": "REFUSED"},
        extracted_facts=facts,
        ai_status="SUCCEEDED",
        ai_recommendation="APPROVE",
        ai_confidence=0.99,
    )


def test_ai_reject_never_enters_safe_approval_bucket() -> None:
    bucket = review_triage_bucket(
        source_type="planning",
        review_status="PENDING",
        metadata={},
        extracted_facts={
            "planning_candidate_matched": False,
            "opportunity_creation_decision": "IGNORE_FOR_OPPORTUNITY",
        },
        ai_status="SUCCEEDED",
        ai_recommendation="REJECT",
        ai_confidence=1.0,
    )
    assert bucket == "MANUAL_REVIEW_REQUIRED"


def test_care_ai_approval_qa_marker_has_dedicated_triage_bucket() -> None:
    bucket = review_triage_bucket(
        signal_id=str(uuid4()),
        vertical="CHILDRENS_HOME",
        source_type="planning",
        review_status="PENDING",
        metadata={"planning_status": "Pending"},
        extracted_facts={
            "care_planning_ai_approval": {
                "policy_version": "care-planning-ai-approval-v1",
                "outcome": "QA_HOLDOUT",
            }
        },
        ai_status="SUCCEEDED",
        ai_recommendation="APPROVE",
        ai_confidence=0.95,
    )
    assert bucket == "QA_HOLDOUT_CARE_AI_APPROVAL"


def test_pending_triage_filter_uses_shared_bucket_logic(monkeypatch) -> None:
    safe_id = uuid4()
    while safe_approval_qa_holdout(str(safe_id)):
        safe_id = uuid4()
    disagree_id = uuid4()
    facts = {
        "planning_candidate_matched": True,
        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
    }
    rows = [
        {
            "id": safe_id,
            "vertical": "NURSERY",
            "source_type": "planning",
            "review_status": "PENDING",
            "metadata": {},
            "extracted_facts": facts,
            "ai_status": "SUCCEEDED",
            "ai_recommendation": "APPROVE",
            "ai_confidence": 0.96,
        },
        {
            "id": disagree_id,
            "vertical": "NURSERY",
            "source_type": "planning",
            "review_status": "PENDING",
            "metadata": {},
            "extracted_facts": facts,
            "ai_status": "SUCCEEDED",
            "ai_recommendation": "REJECT",
            "ai_confidence": 0.99,
        },
    ]

    @contextmanager
    def fake_connection(settings):
        yield object()

    monkeypatch.setattr("app.repository.connection", fake_connection)
    monkeypatch.setattr(
        "app.repository.review_triage_summary",
        lambda settings, vertical: {"recommended_safe_threshold": None},
    )

    def fake_rows(conn, review_status, vertical):
        assert review_status == "PENDING"
        return rows

    monkeypatch.setattr("app.repository._review_triage_rows", fake_rows)
    assert pending_review_triage_ids(
        Settings(), vertical="NURSERY", bucket="SAFE_APPROVE_AGREEMENT"
    ) == [str(safe_id)]
    assert pending_review_triage_ids(
        Settings(), vertical="NURSERY", bucket="DETERMINISTIC_AI_DISAGREE"
    ) == [str(disagree_id)]


def test_disagreement_and_uncertainty_have_separate_triage_buckets() -> None:
    facts = {
        "planning_candidate_matched": True,
        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
    }
    assert (
        review_triage_bucket(
            source_type="planning",
            review_status="PENDING",
            metadata={},
            extracted_facts=facts,
            ai_status="SUCCEEDED",
            ai_recommendation="REJECT",
            ai_confidence=0.99,
        )
        == "DETERMINISTIC_AI_DISAGREE"
    )
    assert (
        review_triage_bucket(
            source_type="planning",
            review_status="PENDING",
            metadata={},
            extracted_facts=facts,
            ai_status="SUCCEEDED",
            ai_recommendation="NEEDS_HUMAN",
            ai_confidence=0.8,
        )
        == "AI_UNCERTAIN"
    )


def test_safe_approval_v1_is_nursery_only_and_threshold_is_fixed() -> None:
    signal_id = str(uuid4())
    facts = {
        "planning_candidate_matched": True,
        "likely_false_positive": False,
        "opportunity_creation_decision": "SUPPORT_EXISTING_ONLY",
    }
    common = {
        "signal_id": signal_id,
        "source_type": "planning",
        "review_status": "PENDING",
        "metadata": {},
        "extracted_facts": facts,
        "ai_status": "SUCCEEDED",
        "ai_recommendation": "APPROVE",
    }
    assert safe_approval_policy_outcome(
        **common, vertical="NURSERY", ai_confidence=0.949
    ) == "INELIGIBLE"
    assert safe_approval_policy_outcome(
        **common, vertical="CHILDRENS_HOME", ai_confidence=0.99
    ) == "INELIGIBLE"
    expected = "QA_HOLDOUT" if safe_approval_qa_bucket(signal_id) == 0 else "AUTO_APPROVE"
    assert safe_approval_policy_outcome(
        **common, vertical="NURSERY", ai_confidence=0.95
    ) == expected


def test_safe_approval_holdout_is_stable_and_refusal_wins() -> None:
    signal_id = str(uuid4())
    assert safe_approval_qa_bucket(signal_id) == safe_approval_qa_bucket(signal_id)
    assert not safe_approval_candidate(
        signal_id=signal_id,
        vertical="NURSERY",
        source_type="planning",
        review_status="PENDING",
        metadata={"decision": "Refusal"},
        extracted_facts={
            "planning_candidate_matched": True,
            "opportunity_creation_decision": "CREATE_OPPORTUNITY",
        },
        ai_status="SUCCEEDED",
        ai_recommendation="APPROVE",
        ai_confidence=1.0,
    )


class RefusalConnection:
    def __init__(self) -> None:
        self.pending = True
        self.statements = []
        self.last_result = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "UPDATE signal_enrichments" in sql and self.pending:
            self.pending = False
            self.last_result = [("CHILDRENS_HOME",)]
        else:
            self.last_result = []
        return self

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self) -> None:
        return None


def test_refusal_review_uses_authoritative_pending_transition_and_audit(monkeypatch) -> None:
    fake = RefusalConnection()

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    signal_id = str(uuid4())
    metadata = {"decision": "Refused", "decision_date": "2026-09-20"}

    assert auto_reject_refused_planning(Settings(), signal_id, metadata) is True
    assert auto_reject_refused_planning(Settings(), signal_id, metadata) is False

    update_sql = next(sql for sql, _ in fake.statements if "UPDATE signal_enrichments" in sql)
    assert "se.review_status = 'PENDING'" in update_sql
    audits = [
        (sql, params) for sql, params in fake.statements if "INSERT INTO admin_audit_events" in sql
    ]
    assert len(audits) == 1
    assert "PLANNING_REFUSAL_AUTO_REJECT" in audits[0][0]
    assert "Automatically rejected" in audits[0][1][1].obj["reason"]
    assert audits[0][1][1].obj["policy_version"] == "planning-refusal-v4"


class SafeApprovalConnection:
    def __init__(self, signal_id: str) -> None:
        self.signal_id = signal_id
        self.status = "PENDING"
        self.statements = []
        self.last_result = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "SELECT rs.id, rs.vertical" in sql:
            self.last_result = [
                (
                    self.signal_id,
                    "NURSERY",
                    "planning",
                    {"planning_status": "Pending"},
                    self.status,
                    {
                        "planning_candidate_matched": True,
                        "likely_false_positive": False,
                        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                    },
                    "SUCCEEDED",
                    "APPROVE",
                    0.95,
                )
            ]
        elif "UPDATE signal_enrichments" in sql and self.status == "PENDING":
            self.status = "APPROVED" if "review_status = 'APPROVED'" in sql else "PENDING"
            self.last_result = [(self.signal_id,)]
        else:
            self.last_result = []
        return self

    def fetchone(self):
        return self.last_result[0] if self.last_result else None

    def commit(self) -> None:
        return None


def test_safe_auto_approval_is_authoritative_audited_and_idempotent(monkeypatch) -> None:
    signal_id = str(uuid4())
    while safe_approval_qa_holdout(signal_id):
        signal_id = str(uuid4())
    fake = SafeApprovalConnection(signal_id)

    @contextmanager
    def fake_connection(settings):
        yield fake

    monkeypatch.setattr("app.repository.connection", fake_connection)
    first = apply_safe_approval_policy(Settings(), signal_id)
    second = apply_safe_approval_policy(Settings(), signal_id)

    assert first == {"signal_id": signal_id, "outcome": "AUTO_APPROVE", "updated": True}
    assert second["updated"] is False
    audits = [sql for sql, _ in fake.statements if "INSERT INTO admin_audit_events" in sql]
    assert len(audits) == 1
    update = next(
        (sql, params)
        for sql, params in fake.statements
        if "review_status = 'APPROVED'" in sql
    )
    assert update[1][0] == "system:safe-approval-v1"
    assert update[1][1].obj["safe_approval"]["policy_version"] == "safe-approval-v1"
