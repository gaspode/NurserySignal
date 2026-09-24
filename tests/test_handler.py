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
