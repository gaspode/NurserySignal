from app.handler import handler


def test_root_endpoint() -> None:
    response = handler({"rawPath": "/", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 200


def test_unknown_endpoint() -> None:
    response = handler({"rawPath": "/missing", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 404


def test_options_preflight_does_not_require_application_authentication() -> None:
    response = handler(
        {
            "rawPath": "/admin/signals",
            "requestContext": {"http": {"method": "OPTIONS"}},
        },
        None,
    )
    assert response["statusCode"] == 204


def test_iam_foundational_evidence_diagnostic_is_read_only(monkeypatch) -> None:
    diagnostic = {"opportunities_newly_foundational": 6}
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: object())
    monkeypatch.setattr(
        "app.handler.care_opportunity_hygiene_audit",
        lambda settings, limit: {"support_semantics_diagnostic": diagnostic},
    )

    result = handler({"operation": "care_foundational_evidence_diagnostic"}, None)

    assert result == diagnostic


def test_iam_evidence_support_verification_is_bounded_and_read_only(monkeypatch) -> None:
    opportunity_id = "1d043a74-dca2-4830-92dd-96aeb9c4f3fe"
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: object())
    monkeypatch.setattr(
        "app.handler.opportunity_detail",
        lambda settings, value: {
            "id": value,
            "evidence_support": {"foundational": 1, "supporting_followups": 0},
            "publication_status": "DRAFT",
        },
    )

    result = handler(
        {
            "operation": "care_evidence_support_verify",
            "opportunity_ids": [opportunity_id],
        },
        None,
    )

    assert result == {
        "items": [
            {
                "opportunity_id": opportunity_id,
                "found": True,
                "evidence_support": {"foundational": 1, "supporting_followups": 0},
                "publication_status": "DRAFT",
            }
        ],
        "read_only": True,
    }


def test_iam_semantic_drift_operation_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr("app.handler.Settings.from_env", lambda: object())
    calls = []
    monkeypatch.setattr(
        "app.handler.care_opportunity_semantic_drift_cleanup",
        lambda settings, **kwargs: calls.append(kwargs) or {"corrected": 2},
    )

    result = handler(
        {
            "operation": "care_opportunity_semantic_drift",
            "apply": True,
            "limit": 999,
            "actor": "test-admin",
        },
        None,
    )

    assert result == {"corrected": 2}
    assert calls == [{"apply": True, "actor": "test-admin", "limit": 25}]
