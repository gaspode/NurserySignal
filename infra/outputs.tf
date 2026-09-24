output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
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

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_app_client_id" {
  value = aws_cognito_user_pool_client.main.id
}

output "github_actions_role_arn" {
  value = var.github_repository == "" ? null : aws_iam_role.github_actions[0].arn
}
