from __future__ import annotations

from uuid import uuid4

from app.care_ai_validation import (
    CARE_PLANNING_PREVIOUS_PROMPT_VERSION,
    care_ai_policy_preview_candidate,
    care_planning_ai_currency,
    care_planning_ai_refresh_preview,
    refresh_stale_care_planning_ai,
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
    assert care_ai_policy_preview_candidate(candidate(metadata={"decision": "Refusal"})) is False
    assert care_ai_policy_preview_candidate(candidate(metadata={"decision": "Withdrawn"})) is False


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


def test_ai_currency_uses_latest_assessment_semantics():
    assert care_planning_ai_currency({}) == "NO_AI_ASSESSMENT"
    assert (
        care_planning_ai_currency(
            {"latest_prompt_version": "care-planning-shadow-v1", "latest_status": "SUCCEEDED"}
        )
        == "STALE_V1"
    )
    assert (
        care_planning_ai_currency(
            {"latest_prompt_version": "care-planning-shadow-v2", "latest_status": "SUCCEEDED"}
        )
        == "CURRENT_V2"
    )
    assert (
        care_planning_ai_currency(
            {"latest_prompt_version": "care-planning-shadow-v2", "latest_status": "FAILED"}
        )
        == "AI_FAILED"
    )


def test_refresh_selects_stale_and_missing_but_skips_current_and_preserves_history(monkeypatch):
    stale_id, missing_id, current_id = (str(uuid4()) for _ in range(3))
    base = {
        "review_status": "PENDING",
        "metadata": {"decision": "Refusal"},
        "extracted_facts": {"planning_subtype": "NEW_HOME_CHANGE_OF_USE"},
    }
    rows = [
        {
            **base,
            "id": stale_id,
            "latest_prompt_version": "care-planning-shadow-v1",
            "latest_status": "SUCCEEDED",
            "latest_recommendation": "APPROVE",
        },
        {
            **base,
            "id": missing_id,
            "latest_prompt_version": None,
            "latest_status": None,
            "latest_recommendation": None,
        },
        {
            **base,
            "id": current_id,
            "latest_prompt_version": "care-planning-shadow-v2",
            "latest_status": "SUCCEEDED",
            "latest_recommendation": "REJECT",
        },
    ]
    saved = []
    monkeypatch.setattr("app.care_ai_validation._rows", lambda *_: rows)
    monkeypatch.setattr(
        "app.care_ai_validation.get_raw_signal",
        lambda _settings, signal_id: {
            "id": signal_id,
            "vertical": "CHILDRENS_HOME",
            "source_type": "planning",
            "metadata": {"decision": "Refusal"},
        },
    )
    monkeypatch.setattr("app.care_ai_validation.get_ai_review", lambda *_: None)
    monkeypatch.setattr(
        "app.care_ai_validation.evaluate_shadow",
        lambda *_: {
            "provider": "BEDROCK",
            "model_id": "model",
            "prompt_version": "care-planning-shadow-v2",
            "status": "SUCCEEDED",
            "recommendation": "REJECT",
            "confidence": 0.95,
            "reason": "Refused",
            "attempted_at": 1,
            "evaluated_at": 1,
        },
    )
    monkeypatch.setattr(
        "app.care_ai_validation.save_ai_review",
        lambda _settings, signal_id, review: saved.append((signal_id, review)) or True,
    )
    monkeypatch.setattr(
        "app.care_ai_validation.record_admin_audit", lambda *args, **kwargs: "audit"
    )

    result = refresh_stale_care_planning_ai(
        Settings(ai_model_id="model"), actor="admin", limit=10, include_missing=True
    )

    assert result["attempted"] == 2
    assert result["succeeded"] == 2
    assert result["structured_negative_corrections"] == 1
    assert [item[0] for item in saved] == [stale_id, missing_id]
    assert all(item[1]["prompt_version"] == "care-planning-shadow-v2" for item in saved)
    assert result["review_decisions_mutated"] is False
    assert result["customer_publication_unchanged"] is True


def test_refresh_is_bounded_and_idempotently_skips_existing_v2(monkeypatch):
    rows = [
        {
            "id": str(uuid4()),
            "review_status": "PENDING",
            "metadata": {},
            "extracted_facts": {},
            "latest_prompt_version": "care-planning-shadow-v1",
            "latest_status": "SUCCEEDED",
            "latest_recommendation": "APPROVE",
        }
        for _ in range(12)
    ]
    monkeypatch.setattr("app.care_ai_validation._rows", lambda *_: rows)
    monkeypatch.setattr(
        "app.care_ai_validation.get_raw_signal",
        lambda *_: {"vertical": "CHILDRENS_HOME", "source_type": "planning"},
    )
    monkeypatch.setattr("app.care_ai_validation.get_ai_review", lambda *_: {"status": "SUCCEEDED"})
    monkeypatch.setattr(
        "app.care_ai_validation.record_admin_audit", lambda *args, **kwargs: "audit"
    )
    result = refresh_stale_care_planning_ai(Settings(), actor="admin", limit=999)
    assert result["batch_limit"] == 10
    assert result["attempted"] == 10
    assert result["idempotent_skips"] == 10


def test_refresh_preview_reports_current_stale_missing_failed_and_subtypes(monkeypatch):
    def row(version, status, recommendation="APPROVE", subtype="AMBIGUOUS"):
        return {
            "review_status": "PENDING",
            "latest_prompt_version": version,
            "latest_status": status,
            "latest_recommendation": recommendation,
            "latest_confidence": 0.95,
            "extracted_facts": {"planning_subtype": subtype},
        }

    monkeypatch.setattr(
        "app.care_ai_validation._rows",
        lambda *_: [
            row("care-planning-shadow-v2", "SUCCEEDED", subtype="NEW_HOME_CHANGE_OF_USE"),
            row("care-planning-shadow-v1", "SUCCEEDED"),
            row(None, None),
            row("care-planning-shadow-v2", "FAILED", recommendation=None),
        ],
    )
    result = care_planning_ai_refresh_preview(Settings())
    assert result["pending_total"] == 4
    assert result["currency"] == {
        "CURRENT_V2": 1,
        "STALE_V1": 1,
        "NO_AI_ASSESSMENT": 1,
        "AI_FAILED": 1,
    }
    assert result["v2_recommendations"] == {"APPROVE": 1}


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
    monkeypatch.setattr("app.care_ai_validation.get_signal_review_status", lambda *_: "REJECTED")
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
