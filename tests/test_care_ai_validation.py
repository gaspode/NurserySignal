from __future__ import annotations

from uuid import uuid4

from app.care_ai_validation import (
    CARE_PLANNING_PREVIOUS_PROMPT_VERSION,
    care_ai_policy_preview_candidate,
    run_care_planning_ai_validation,
)
from app.config import Settings


def candidate(**updates):
    value = {
        "id": "6ee8f942-9768-4c6e-ab35-3ac00483d94c",
        "review_status": "PENDING",
        "reviewed_by": None,
        "metadata": {"decision": "Grant Conditionally"},
        "latest_status": "SUCCEEDED",
        "latest_recommendation": "APPROVE",
        "latest_confidence": 0.95,
        "extracted_facts": {
            "likely_false_positive": False,
            "planning_ambiguity_markers": [],
            "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
        },
    }
    value.update(updates)
    return value


def test_policy_preview_accepts_only_conservative_pending_candidate():
    assert care_ai_policy_preview_candidate(candidate()) is True
    assert care_ai_policy_preview_candidate(candidate(latest_confidence=0.949)) is False
    assert (
        care_ai_policy_preview_candidate(candidate(metadata={"decision": "Refusal"})) is False
    )
    assert (
        care_ai_policy_preview_candidate(candidate(metadata={"decision": "Withdrawn"})) is False
    )


def test_policy_preview_excludes_ambiguous_and_followup_subtypes():
    assert (
        care_ai_policy_preview_candidate(
            candidate(
                extracted_facts={
                    "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
                    "planning_ambiguity_markers": ["unclear use"],
                }
            )
        )
        is False
    )
    for subtype in (
        "LAWFULNESS_EXISTING",
        "CONDITION_DISCHARGE",
        "NON_MATERIAL_AMENDMENT",
    ):
        assert (
            care_ai_policy_preview_candidate(
                candidate(extracted_facts={"planning_subtype": subtype})
            )
            is False
        )


def test_policy_preview_does_not_mutate_or_enable_automation():
    value = candidate()
    original = dict(value)
    assert care_ai_policy_preview_candidate(value) is True
    assert value == original


def test_validation_saves_v2_beside_historical_v1_without_review_mutation(monkeypatch):
    signal_id = str(uuid4())
    saved = []
    audits = []
    monkeypatch.setattr(
        "app.care_ai_validation.get_raw_signal",
        lambda *_: {"vertical": "CHILDRENS_HOME", "source_type": "planning"},
    )

    def review(_settings, _signal_id, _model, version):
        if version == CARE_PLANNING_PREVIOUS_PROMPT_VERSION:
            return {"recommendation": "APPROVE"}
        return None

    monkeypatch.setattr("app.care_ai_validation.get_ai_review", review)
    monkeypatch.setattr(
        "app.care_ai_validation.evaluate_shadow",
        lambda *_: {
            "provider": "BEDROCK",
            "model_id": "model",
            "prompt_version": "care-planning-shadow-v2",
            "status": "SUCCEEDED",
            "recommendation": "REJECT",
            "confidence": 0.99,
            "reason": "Refused.",
        },
    )
    monkeypatch.setattr(
        "app.care_ai_validation.save_ai_review",
        lambda _settings, _signal_id, value: saved.append(value) or True,
    )
    monkeypatch.setattr(
        "app.care_ai_validation.get_signal_review_status", lambda *_: "REJECTED"
    )
    monkeypatch.setattr(
        "app.care_ai_validation.record_admin_audit",
        lambda *args, **kwargs: audits.append(kwargs) or "audit",
    )

    result = run_care_planning_ai_validation(
        Settings(ai_model_id="model"), signal_ids=[signal_id], actor="admin"
    )

    assert result["review_decisions_mutated"] is False
    assert result["items"][0]["v1_recommendation"] == "APPROVE"
    assert result["items"][0]["v2_recommendation"] == "REJECT"
    assert saved[0]["prompt_version"] == "care-planning-shadow-v2"
    assert audits[0]["details"]["review_decisions_mutated"] is False
