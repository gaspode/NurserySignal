from __future__ import annotations

import json
from uuid import uuid4

from app.config import Settings
from app.handler import handler
from app.shadow_review import reevaluate_ai_shadow


def event(path: str, claims: dict | None = None) -> dict:
    default_claims = {"sub": "admin", "cognito:groups": ["NurserySignalAdmins"]}
    return {
        "rawPath": path,
        "body": None,
        "requestContext": {
            "http": {"method": "POST"},
            "authorizer": {"jwt": {"claims": claims or default_claims}},
        },
    }


def review(status: str = "SUCCEEDED") -> dict:
    return {
        "provider": "BEDROCK",
        "model_id": "amazon.nova-lite-v1:0",
        "prompt_version": "shadow-v1",
        "status": status,
        "recommendation": "APPROVE" if status == "SUCCEEDED" else None,
        "confidence": 0.91 if status == "SUCCEEDED" else None,
        "reason": "Explicit new childcare provision." if status == "SUCCEEDED" else None,
        "failure_category": None if status == "SUCCEEDED" else "THROTTLED",
        "attempted_at": 1_700_000_000,
        "evaluated_at": 1_700_000_001 if status == "SUCCEEDED" else None,
        "latency_ms": 42,
    }


def test_ai_review_endpoint_requires_admin(monkeypatch):
    response = handler(event(f"/admin/signals/{uuid4()}/ai-review", {"sub": "user"}), None)
    assert response["statusCode"] == 403


def test_ai_review_endpoint_returns_shadow_result(monkeypatch):
    signal_id = str(uuid4())
    expected = {**review(), "idempotent": False, "human_review_status": "PENDING"}
    monkeypatch.setattr("app.handler.reevaluate_ai_shadow", lambda settings, value: expected)
    response = handler(event(f"/admin/signals/{signal_id}/ai-review"), None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == expected


def test_re_evaluation_is_idempotent_and_does_not_call_bedrock(monkeypatch):
    signal_id = str(uuid4())
    stored = {"id": uuid4(), **review()}
    monkeypatch.setattr(
        "app.shadow_review.get_raw_signal", lambda settings, value: {"id": signal_id}
    )
    monkeypatch.setattr("app.shadow_review.get_ai_review", lambda *args: stored)
    monkeypatch.setattr("app.shadow_review.get_signal_review_status", lambda *args: "PENDING")
    monkeypatch.setattr(
        "app.shadow_review.evaluate_shadow",
        lambda *args: (_ for _ in ()).throw(AssertionError("Bedrock must not be called")),
    )
    result = reevaluate_ai_shadow(Settings(), signal_id)
    assert result["idempotent"] is True
    assert result["human_review_status"] == "PENDING"
    assert result["recommendation"] == "APPROVE"


def test_re_evaluation_saves_success_without_changing_review_status(monkeypatch):
    signal_id = str(uuid4())
    saved = []
    monkeypatch.setattr(
        "app.shadow_review.get_raw_signal", lambda settings, value: {"id": signal_id}
    )
    monkeypatch.setattr("app.shadow_review.get_ai_review", lambda *args: None)
    monkeypatch.setattr("app.shadow_review.get_signal_review_status", lambda *args: "PENDING")
    monkeypatch.setattr("app.shadow_review.evaluate_shadow", lambda *args: review())
    monkeypatch.setattr(
        "app.shadow_review.save_ai_review", lambda *args: saved.append(args[2]) or True
    )
    result = reevaluate_ai_shadow(Settings(), signal_id)
    assert result["status"] == "SUCCEEDED"
    assert result["human_review_status"] == "PENDING"
    assert len(saved) == 1


def test_bedrock_failure_is_saved_without_changing_review_status(monkeypatch):
    signal_id = str(uuid4())
    failed = review("FAILED")
    saved = []
    monkeypatch.setattr(
        "app.shadow_review.get_raw_signal", lambda settings, value: {"id": signal_id}
    )
    monkeypatch.setattr("app.shadow_review.get_ai_review", lambda *args: None)
    monkeypatch.setattr("app.shadow_review.get_signal_review_status", lambda *args: "PENDING")
    monkeypatch.setattr("app.shadow_review.evaluate_shadow", lambda *args: failed)
    monkeypatch.setattr(
        "app.shadow_review.save_ai_review", lambda *args: saved.append(args[2]) or True
    )
    result = reevaluate_ai_shadow(Settings(), signal_id)
    assert result["status"] == "FAILED"
    assert result["failure_category"] == "THROTTLED"
    assert result["human_review_status"] == "PENDING"
    assert len(saved) == 1
