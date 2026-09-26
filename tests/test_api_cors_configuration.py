from pathlib import Path

API_TERRAFORM = Path(__file__).parents[1] / "infra" / "api.tf"


def test_cors_configuration_is_restricted_to_admin_frontend_and_required_headers() -> None:
    source = API_TERRAFORM.read_text()

    assert (
        'allow_origins = ["https://${aws_cloudfront_distribution.frontend.domain_name}"]' in source
    )
    assert 'allow_headers = ["content-type", "authorization"]' in source
    assert 'allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]' in source


def test_preflight_route_is_unauthenticated_while_admin_route_uses_cognito() -> None:
    source = API_TERRAFORM.read_text()
    options_start = source.index('resource "aws_apigatewayv2_route" "admin_options"')
    options_block = source[options_start:]
    admin_start = source.index('resource "aws_apigatewayv2_route" "admin"')
    admin_block = source[admin_start:options_start]

    assert 'route_key          = "OPTIONS /admin/{proxy+}"' in options_block
    assert 'authorization_type = "NONE"' in options_block
    assert 'route_key          = "ANY /admin/{proxy+}"' in admin_block
    assert 'authorization_type = "JWT"' in admin_block
