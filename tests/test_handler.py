import json

from app.handler import handler


def test_root_endpoint() -> None:
    response = handler({"rawPath": "/", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 200


def test_unknown_endpoint() -> None:
    response = handler({"rawPath": "/missing", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 404


def test_operations_summary_is_admin_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.operations_summary",
        lambda settings: {"schema_version": "signalhub-operations-summary-v1", "read_only": True},
    )
    base = {
        "rawPath": "/admin/operations/summary",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"jwt": {"claims": {"sub": "staff"}}},
        },
    }
    denied = handler(base, None)
    assert denied["statusCode"] == 403
    allowed = {
        **base,
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {
                "jwt": {"claims": {"sub": "admin", "cognito:groups": ["NurserySignalAdmins"]}}
            },
        },
    }
    response = handler(allowed, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["read_only"] is True


def test_withdrawal_preview_is_admin_only_and_read_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.handler.care_withdrawal_preview",
        lambda settings: {
            "preview_only": True,
            "enabled": False,
            "publication_state_mutations": 0,
        },
    )
    base = {
        "rawPath": "/admin/opportunities/withdrawal-preview",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {"jwt": {"claims": {"sub": "staff"}}},
        },
    }
    assert handler(base, None)["statusCode"] == 403
    allowed = {
        **base,
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {
                "jwt": {"claims": {"sub": "admin", "cognito:groups": ["NurserySignalAdmins"]}}
            },
        },
    }
    response = handler(allowed, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["publication_state_mutations"] == 0


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
            "signals": [
                {
                    "id": "signal-1",
                    "source_type": "planning",
                    "planning_outcome": "APPROVED",
                    "planning_decision_raw": "Grant Conditionally",
                    "planning_status_raw": "Decided",
                    "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
                    "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                    "evidence_support_classification": "FOUNDATIONAL",
                    "planning_consistency_warning": None,
                },
                {"id": "signal-2", "source_type": "recruitment"},
            ],
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
                "planning_timeline": [
                    {
                        "signal_id": "signal-1",
                        "planning_outcome": "APPROVED",
                        "planning_decision_raw": "Grant Conditionally",
                        "planning_status_raw": "Decided",
                        "planning_subtype": "NEW_HOME_CHANGE_OF_USE",
                        "opportunity_creation_decision": "CREATE_OPPORTUNITY",
                        "evidence_support_classification": "FOUNDATIONAL",
                        "planning_consistency_warning": None,
                    }
                ],
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
