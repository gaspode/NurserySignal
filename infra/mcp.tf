locals {
  mcp_resource_url       = "${aws_apigatewayv2_api.http.api_endpoint}/mcp"
  mcp_read_scope         = "signalhub-mcp/read"
  mcp_oauth_domain       = "${local.name_prefix}-mcp-${data.aws_caller_identity.current.account_id}"
  mcp_oauth_callback_url = "${aws_apigatewayv2_api.http.api_endpoint}/oauth/callback"
}

resource "aws_cognito_user_pool_domain" "mcp" {
  domain       = local.mcp_oauth_domain
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_resource_server" "mcp" {
  identifier   = "signalhub-mcp"
  name         = "SignalHub MCP"
  user_pool_id = aws_cognito_user_pool.main.id
  scope {
    scope_name        = "read"
    scope_description = "Read-only SignalHub administration and operations"
  }
}

data "http" "mcp_token_jwks" {
  url = "https://${aws_cognito_user_pool.main.endpoint}/.well-known/jwks.json"
  request_headers = {
    Accept = "application/json"
  }
  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "Cognito JWKS could not be loaded for MCP token validation."
    }
  }
}

resource "aws_cognito_user_pool_client" "mcp_chatgpt" {
  name                                 = "${local.name_prefix}-mcp-chatgpt"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  supported_identity_providers         = ["COGNITO"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", local.mcp_read_scope]
  callback_urls                        = distinct(concat(var.mcp_oauth_callback_urls, [local.mcp_oauth_callback_url]))
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
  depends_on = [aws_cognito_resource_server.mcp]
}

resource "aws_cognito_user_pool_client" "mcp_service" {
  name                                 = "${local.name_prefix}-mcp-service"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_scopes                 = [local.mcp_read_scope]
  access_token_validity                = 60
  token_validity_units {
    access_token = "minutes"
  }
  depends_on = [aws_cognito_resource_server.mcp]
}

resource "aws_secretsmanager_secret" "mcp_readonly" {
  name                    = "${local.name_prefix}/mcp-readonly"
  description             = "SignalHub MCP v1 scoped service OAuth client"
  recovery_window_in_days = 7
  tags                    = local.common_tags
  depends_on              = [aws_iam_policy.github_actions]
}

resource "aws_secretsmanager_secret_version" "mcp_readonly" {
  secret_id = aws_secretsmanager_secret.mcp_readonly.id
  secret_string = jsonencode({
    client_id     = aws_cognito_user_pool_client.mcp_service.id
    client_secret = aws_cognito_user_pool_client.mcp_service.client_secret
    scope         = local.mcp_read_scope
    token_url     = "https://${aws_cognito_user_pool_domain.mcp.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
    version       = 1
  })
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/aws/lambda/${local.name_prefix}-mcp"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_dynamodb_table" "mcp_oauth_transactions" {
  name         = "${local.name_prefix}-mcp-oauth-transactions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "state"
  attribute {
    name = "state"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
  tags       = local.common_tags
  depends_on = [aws_iam_policy.github_actions]
}

resource "aws_cloudwatch_log_group" "mcp_oauth" {
  name              = "/aws/lambda/${local.name_prefix}-mcp-oauth"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "mcp" {
  name               = "${local.name_prefix}-mcp"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "mcp_vpc" {
  role       = aws_iam_role.mcp.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "mcp_readonly" {
  name = "${local.name_prefix}-mcp-readonly"
  role = aws_iam_role.mcp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [aws_secretsmanager_secret.database.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.source_runs.arn
      }
    ]
  })
}

resource "aws_iam_role" "mcp_oauth" {
  name               = "${local.name_prefix}-mcp-oauth"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
  depends_on         = [aws_iam_policy.github_actions]
}

resource "aws_iam_role_policy_attachment" "mcp_oauth_basic" {
  role       = aws_iam_role.mcp_oauth.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "mcp_oauth_transactions" {
  name = "${local.name_prefix}-mcp-oauth-transactions"
  role = aws_iam_role.mcp_oauth.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:PutItem",
        "dynamodb:DeleteItem",
      ]
      Resource = aws_dynamodb_table.mcp_oauth_transactions.arn
    }]
  })
}

resource "aws_lambda_function" "mcp" {
  function_name    = "${local.name_prefix}-mcp"
  role             = aws_iam_role.mcp.arn
  runtime          = "python3.12"
  handler          = "app.mcp.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256
  vpc_config {
    subnet_ids         = local.lambda_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }
  environment {
    variables = {
      ADMIN_GROUP                    = aws_cognito_user_group.administrators.name
      APP_ENV                        = var.environment
      SERVICE_NAME                   = "${local.name_prefix}-mcp"
      DB_SECRET_ARN                  = aws_secretsmanager_secret.database.arn
      MCP_RESOURCE_URL               = local.mcp_resource_url
      MCP_OAUTH_ISSUER               = aws_apigatewayv2_api.http.api_endpoint
      MCP_OAUTH_AUTHORIZATION_SERVER = "https://${aws_cognito_user_pool_domain.mcp.domain}.auth.${var.aws_region}.amazoncognito.com"
      MCP_TOKEN_ISSUER               = "https://${aws_cognito_user_pool.main.endpoint}"
      MCP_TOKEN_JWKS                 = data.http.mcp_token_jwks.response_body
      MCP_OAUTH_CALLBACK_URL         = local.mcp_oauth_callback_url
      MCP_USER_CLIENT_ID             = aws_cognito_user_pool_client.mcp_chatgpt.id
      MCP_SERVICE_CLIENT_ID          = aws_cognito_user_pool_client.mcp_service.id
      MCP_RATE_LIMIT_PER_MINUTE      = "60"
      SOURCE_RUNS_TABLE_NAME         = aws_dynamodb_table.source_runs.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.mcp, aws_vpc_endpoint.secretsmanager]
  tags       = local.common_tags
}

resource "aws_lambda_function" "mcp_oauth" {
  function_name    = "${local.name_prefix}-mcp-oauth"
  role             = aws_iam_role.mcp_oauth.arn
  runtime          = "python3.12"
  handler          = "app.mcp.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30
  memory_size      = 256
  environment {
    variables = {
      ADMIN_GROUP                       = aws_cognito_user_group.administrators.name
      APP_ENV                           = var.environment
      SERVICE_NAME                      = "${local.name_prefix}-mcp-oauth"
      MCP_RESOURCE_URL                  = local.mcp_resource_url
      MCP_OAUTH_ISSUER                  = aws_apigatewayv2_api.http.api_endpoint
      MCP_OAUTH_AUTHORIZATION_SERVER    = "https://${aws_cognito_user_pool_domain.mcp.domain}.auth.${var.aws_region}.amazoncognito.com"
      MCP_TOKEN_ISSUER                  = "https://${aws_cognito_user_pool.main.endpoint}"
      MCP_TOKEN_JWKS                    = data.http.mcp_token_jwks.response_body
      MCP_OAUTH_CALLBACK_URL            = local.mcp_oauth_callback_url
      MCP_OAUTH_TRANSACTIONS_TABLE_NAME = aws_dynamodb_table.mcp_oauth_transactions.name
      MCP_USER_CLIENT_ID                = aws_cognito_user_pool_client.mcp_chatgpt.id
      MCP_SERVICE_CLIENT_ID             = aws_cognito_user_pool_client.mcp_service.id
    }
  }
  depends_on = [
    aws_cloudwatch_log_group.mcp_oauth,
    aws_iam_role_policy_attachment.mcp_oauth_basic,
    aws_iam_role_policy.mcp_oauth_transactions,
  ]
  tags = local.common_tags
}

resource "aws_apigatewayv2_integration" "mcp" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.mcp.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_integration" "mcp_oauth" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.mcp_oauth.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "mcp" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /mcp"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_proxy" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "ANY /mcp/{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_metadata" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /.well-known/oauth-protected-resource"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_metadata_path" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /.well-known/oauth-protected-resource/mcp"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_server_metadata" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /.well-known/oauth-authorization-server"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_oidc_metadata" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /.well-known/openid-configuration"
  target    = "integrations/${aws_apigatewayv2_integration.mcp.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_authorize" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /oauth/authorize"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_oauth.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_callback" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /oauth/callback"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_oauth.id}"
}

resource "aws_apigatewayv2_route" "mcp_oauth_token" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /oauth/token"
  target    = "integrations/${aws_apigatewayv2_integration.mcp_oauth.id}"
}

resource "aws_lambda_permission" "mcp_api" {
  statement_id  = "AllowMcpHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_lambda_permission" "mcp_oauth_api" {
  statement_id  = "AllowMcpOAuthHttpApiInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_oauth.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
