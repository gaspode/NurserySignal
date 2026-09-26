from __future__ import annotations

import json
from uuid import uuid4

from app.ai_shadow import evaluate_shadow
from app.config import Settings
from app.worker import handler


def settings() -> Settings:
    return Settings(
        ai_shadow_enabled=True, ai_model_id="amazon.nova-lite-v1:0", ai_prompt_version="shadow-v1"
    )


def raw() -> dict:
    return {
        "id": uuid4(),
        "schema_version": "1.0",
        "source_type": "planning",
        "source_url": "https://example.test/planning/1",
        "external_id": "p-1",
        "discovered_at": "2026-09-25T00:00:00Z",
        "title": "New day nursery",
        "raw_text": "A new children's day nursery is proposed.",
        "location_hint": "Bristol",
        "organisation_hint": "Little Acorns",
        "metadata": {"council": "Bristol"},
    }


class BedrockClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def response(recommendation="APPROVE", confidence=0.91):
    return {
        "output": {
            "message": {
                "content": [
                    {
                        "text": json.dumps(
                            {
                                "recommendation": recommendation,
                                "confidence": confidence,
                                "reason": "Explicit new childcare provision.",
                            }
                        )
                    }
                ]
            }
        },
        "usage": {"inputTokens": 20, "outputTokens": 10},
    }


def recruitment_raw(relevance="RELEVANT_ROUTINE") -> dict:
    value = raw()
    value.update(
        {
            "source_type": "recruitment",
            "title": "Early Years Educator",
            "raw_text": "Early Years Educator at Alexandra Preschool.",
            "metadata": {
                "recruitment_classification": {
                    "relevance": relevance,
                    "commercial_change_evidence": "NONE",
                    "role_category": "early_years_educator",
                    "setting_category": "preschool",
                }
            },
        }
    )
    return value


def recruitment_response(recommendation="APPROVE", relevance="RELEVANT_ROUTINE"):
    value = response(recommendation)
    payload = json.loads(value["output"]["message"]["content"][0]["text"])
    payload["recruitment_relevance"] = relevance
    payload["commercial_change_evidence"] = "NONE"
    value["output"]["message"]["content"][0]["text"] = json.dumps(payload)
    return value


def test_shadow_accepts_each_allowed_recommendation(monkeypatch):
    for recommendation in ("APPROVE", "REJECT", "NEEDS_HUMAN"):
        client = BedrockClient(response(recommendation))
        monkeypatch.setattr(
            "app.ai_shadow.boto3.client", lambda *args, _client=client, **kwargs: _client
        )
        result = evaluate_shadow(raw(), settings())
        assert result["recommendation"] == recommendation
        assert result["status"] == "SUCCEEDED"
        assert result["input_tokens"] == 20
        assert client.calls[0]["modelId"] == "amazon.nova-lite-v1:0"
        assert client.calls[0]["inferenceConfig"]["temperature"] == 0.0


def test_shadow_v2_records_commercial_change_evidence(monkeypatch):
    client = BedrockClient(
        {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "recommendation": "APPROVE",
                                    "confidence": 0.85,
                                    "reason": "Relevant recruitment at a nursery-school setting.",
                                    "commercial_change_evidence": "NONE",
                                }
                            )
                        }
                    ]
                }
            }
        }
    )
    monkeypatch.setattr("app.ai_shadow.boto3.client", lambda *args, **kwargs: client)
    result = evaluate_shadow(raw(), Settings(ai_prompt_version="shadow-v2"))
    assert result["prompt_version"] == "shadow-v2"
    assert result["commercial_change_evidence"] == "NONE"


def test_shadow_v3_routine_recruitment_is_approved_even_without_growth_evidence(monkeypatch):
    client = BedrockClient(recruitment_response("REJECT"))
    monkeypatch.setattr("app.ai_shadow.boto3.client", lambda *args, **kwargs: client)
    result = evaluate_shadow(
        recruitment_raw(), Settings(ai_prompt_version="shadow-v3")
    )
    assert result["prompt_version"] == "shadow-v3"
    assert result["recommendation"] == "APPROVE"
    assert result["recruitment_relevance"] == "RELEVANT_ROUTINE"
    assert result["commercial_change_evidence"] == "NONE"


def test_shadow_v3_requires_recruitment_relevance_and_preserves_change_dimension(monkeypatch):
    client = BedrockClient(recruitment_response("APPROVE", "RELEVANT_CHANGE"))
    monkeypatch.setattr("app.ai_shadow.boto3.client", lambda *args, **kwargs: client)
    result = evaluate_shadow(
        recruitment_raw("RELEVANT_CHANGE"), Settings(ai_prompt_version="shadow-v3")
    )
    assert result["recommendation"] == "APPROVE"
    assert result["recruitment_relevance"] == "RELEVANT_CHANGE"
    assert result["commercial_change_evidence"] == "NONE"


def test_shadow_rejects_malformed_or_invalid_confidence(monkeypatch):
    client = BedrockClient(response(confidence=1.5))
    monkeypatch.setattr("app.ai_shadow.boto3.client", lambda *args, **kwargs: client)
    result = evaluate_shadow(raw(), settings())
    assert result["status"] == "FAILED"
    assert result["failure_category"] == "MALFORMED_RESPONSE"


def test_shadow_failure_is_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("not logged")

    monkeypatch.setattr("app.ai_shadow.boto3.client", fail)
    result = evaluate_shadow(raw(), settings())
    assert result["status"] == "FAILED"
    assert result["recommendation"] is None


def test_worker_keeps_deterministic_enrichment_when_ai_fails(monkeypatch):
    signal = raw()
    message = {
        "message_version": "1.0",
        "signal_id": str(signal["id"]),
        "schema_version": "1.0",
        "evidence_bucket": "b",
        "evidence_key": "k",
        "queued_at": "2026-09-25T00:00:00Z",
    }
    saved = []
    ai_saved = []
    monkeypatch.setattr("app.worker.Settings.from_env", lambda: settings())
    monkeypatch.setattr("app.worker.get_raw_signal", lambda settings, value: signal)
    monkeypatch.setattr(
        "app.worker.save_enrichment", lambda settings, value: saved.append(value) or True
    )
    monkeypatch.setattr("app.worker.ai_review_exists", lambda *args: False)
    monkeypatch.setattr(
        "app.worker.evaluate_shadow",
        lambda *args: {
            "provider": "BEDROCK",
            "model_id": "m",
            "prompt_version": "p",
            "status": "FAILED",
            "failure_category": "THROTTLED",
            "attempted_at": 1,
        },
    )
    monkeypatch.setattr(
        "app.worker.save_ai_review",
        lambda settings, signal_id, value: ai_saved.append(value) or True,
    )
    result = handler({"Records": [{"messageId": "m1", "body": json.dumps(message)}]}, None)
    assert result == {"batchItemFailures": []}
    assert saved[0]["confidence"] == 0.86
    assert ai_saved[0]["status"] == "FAILED"


def test_worker_does_not_repeat_same_model_prompt(monkeypatch):
    signal = raw()
    message = {
        "message_version": "1.0",
        "signal_id": str(signal["id"]),
        "schema_version": "1.0",
        "evidence_bucket": "b",
        "evidence_key": "k",
        "queued_at": "2026-09-25T00:00:00Z",
    }
    calls = []
    monkeypatch.setattr("app.worker.Settings.from_env", lambda: settings())
    monkeypatch.setattr("app.worker.get_raw_signal", lambda settings, value: signal)
    monkeypatch.setattr("app.worker.save_enrichment", lambda *args: True)
    monkeypatch.setattr("app.worker.ai_review_exists", lambda *args: True)
    monkeypatch.setattr("app.worker.evaluate_shadow", lambda *args: calls.append(True))
    result = handler({"Records": [{"messageId": "m1", "body": json.dumps(message)}]}, None)
    assert result == {"batchItemFailures": []}
    assert calls == []
