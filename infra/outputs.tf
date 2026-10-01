output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "mcp_url" {
  description = "SignalHub MCP v1 Streamable HTTP endpoint."
  value       = "${aws_apigatewayv2_stage.default.invoke_url}mcp"
}

output "mcp_readonly_secret_arn" {
  description = "Secrets Manager ARN for the scoped read-only MCP service OAuth client."
  value       = aws_secretsmanager_secret.mcp_readonly.arn
}

output "mcp_chatgpt_client_id" {
  description = "Public Cognito OAuth client ID for ChatGPT MCP authorization-code + PKCE."
  value       = aws_cognito_user_pool_client.mcp_chatgpt.id
}

output "mcp_oauth_authorization_url" {
  description = "OAuth authorization endpoint for the SignalHub MCP."
  value       = "https://${aws_cognito_user_pool_domain.mcp.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/authorize"
}

output "mcp_oauth_token_url" {
  description = "OAuth token endpoint for the SignalHub MCP."
  value       = "https://${aws_cognito_user_pool_domain.mcp.domain}.auth.${var.aws_region}.amazoncognito.com/oauth2/token"
}

output "frontend_url" {
  value = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.bucket
}

output "frontend_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}

output "careprospect_nameservers" {
  description = "Authoritative nameservers used to delegate careprospect.co.uk."
  value       = aws_route53_zone.careprospect.name_servers
}

output "raw_evidence_bucket" {
  value = aws_s3_bucket.raw_evidence.bucket
}

output "database_identifier" {
  value = aws_db_instance.main.identifier
}

output "database_endpoint" {
  value = aws_db_instance.main.address
}

output "ingestion_queue_url" {
  value = aws_sqs_queue.ingestion.url
}

output "enrichment_queue_url" {
  value = aws_sqs_queue.enrichment.url
}

output "enrichment_function_name" {
  value = aws_lambda_function.enrichment.function_name
}

output "planning_collector_function_name" {
  value = aws_lambda_function.planning_collector.function_name
}

output "planning_provider_secret_arn" {
  value = aws_secretsmanager_secret.planning_provider.arn
}

output "recruitment_collector_function_name" {
  value = aws_lambda_function.recruitment_collector.function_name
}

output "recruitment_provider_secret_arn" {
  value = aws_secretsmanager_secret.recruitment_provider.arn
}

output "ofsted_collector_function_name" {
  value = aws_lambda_function.ofsted_collector.function_name
}

output "companies_house_collector_function_name" {
  value = aws_lambda_function.companies_house_collector.function_name
}

output "procurement_collector_function_name" {
  value = aws_lambda_function.procurement_collector.function_name
}

output "companies_house_secret_arn" {
  value = aws_secretsmanager_secret.companies_house.arn
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_app_client_id" {
  value = aws_cognito_user_pool_client.main.id
}

output "cognito_admin_group" {
  value = aws_cognito_user_group.administrators.name
}

output "cognito_customer_group" {
  value = aws_cognito_user_group.caresignal_customers.name
}

output "caresignal_customer_url" {
  description = "CareProspect customer portal URL (role-routed after login)."
  value       = "https://careprospect.co.uk/#/care/opportunities"
}

output "customer_weekly_digest_enabled" {
  description = "Whether weekly CareProspect email delivery has a configured sender."
  value       = var.caresignal_email_from != ""
}

output "customer_provisioning_queue_url" {
  description = "Bounded CareProspect customer invitation queue."
  value       = aws_sqs_queue.customer_provisioning.url
}

output "github_actions_role_arn" {
  value = var.github_repository == "" ? null : aws_iam_role.github_actions[0].arn
}
