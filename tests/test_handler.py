from app.handler import handler


def test_root_endpoint() -> None:
    response = handler({"rawPath": "/", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 200


def test_unknown_endpoint() -> None:
    response = handler({"rawPath": "/missing", "requestContext": {"http": {"method": "GET"}}}, None)
    assert response["statusCode"] == 404
