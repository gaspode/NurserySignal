from contextlib import contextmanager
from uuid import uuid4

from app.config import Settings
from app.repository import auto_reject_refused_planning, pending_review_triage_ids
from app.review_triage import (
    REFUSAL_POLICY_VERSION,
    deterministic_review_recommendation,
    normalize_planning_decision,
    planning_refusal_assessment,
    review_triage_bucket,
    safe_approval_candidate,
)


def test_structured_refusal_variants_are_unambiguous() -> None:
    for value in (
        "REFUSED",
        "rejected",
        "Permission: refused",
        "Application refused.",
        "Refuse Permission/Consent",
        "Refuse Permission",
        "Refuse Consent",
        "Refusal of Permission",
        "Refusal of Consent",
        "Permission/Consent Refused",
    ):
        result = planning_refusal_assessment(
            {"planning_status": value, "decision_date": "2026-04-03"}
        )
        assert result.refused is True
        assert result.decision_date == "2026-04-03"
    assert normalize_planning_decision("Permission: refused.") == "PERMISSION REFUSED"
    assert REFUSAL_POLICY_VERSION == "planning-refusal-v2"


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
        "Appeal dismissed",
    ):
        assert planning_refusal_assessment({"decision": value}).refused is False


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


def test_pending_triage_filter_uses_shared_bucket_logic(monkeypatch) -> None:
    safe_id = uuid4()
    disagree_id = uuid4()
    facts = {
        "planning_candidate_matched": True,
        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
    }
    rows = [
        {
            "id": safe_id,
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
    assert audits[0][1][1].obj["policy_version"] == "planning-refusal-v2"
