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
