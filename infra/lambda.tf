data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../build/lambda"
  output_path = "${path.module}/../build/nurserysignal-lambda.zip"
}

resource "aws_cloudwatch_log_group" "backend" {
  name              = "/aws/lambda/${local.name_prefix}-backend"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/aws/lambda/${local.name_prefix}-migration"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_lambda_function" "backend" {
  function_name    = "${local.name_prefix}-backend"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "app.handler.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 15
  memory_size      = 256

  vpc_config {
    subnet_ids         = local.lambda_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      APP_ENV              = var.environment
      SERVICE_NAME         = "${local.name_prefix}-api"
      DB_SECRET_ARN        = aws_secretsmanager_secret.database.arn
      EVIDENCE_BUCKET      = aws_s3_bucket.raw_evidence.bucket
      ENRICHMENT_QUEUE_URL = aws_sqs_queue.enrichment.url
      ADMIN_GROUP          = aws_cognito_user_group.administrators.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.backend]
  tags       = local.common_tags
}

resource "aws_lambda_function" "migration" {
  function_name    = "${local.name_prefix}-migration"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "app.migrations.handler"
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
      APP_ENV       = var.environment
      SERVICE_NAME  = "${local.name_prefix}-migration"
      DB_SECRET_ARN = aws_secretsmanager_secret.database.arn
    }
  }

  depends_on = [aws_cloudwatch_log_group.migration]
  tags       = local.common_tags
}

resource "aws_lambda_invocation" "migration" {
  function_name = aws_lambda_function.migration.function_name
  input = jsonencode({
    migration_bundle = data.archive_file.lambda.output_base64sha256
  })
  depends_on = [aws_db_instance.main, aws_vpc_endpoint.secretsmanager]
}

resource "aws_cloudwatch_log_group" "enrichment" {
  name              = "/aws/lambda/${local.name_prefix}-enrichment"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_lambda_function" "enrichment" {
  function_name    = "${local.name_prefix}-enrichment"
  role             = aws_iam_role.enrichment.arn
  runtime          = "python3.12"
  handler          = "app.worker.handler"
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
      APP_ENV           = var.environment
      SERVICE_NAME      = "${local.name_prefix}-enrichment"
      DB_SECRET_ARN     = aws_secretsmanager_secret.database.arn
      AI_SHADOW_ENABLED = tostring(var.ai_shadow_enabled)
      AI_MODEL_ID       = var.ai_model_id
      AI_PROMPT_VERSION = var.ai_prompt_version
    }
  }

  depends_on = [aws_cloudwatch_log_group.enrichment, aws_vpc_endpoint.bedrock_runtime]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "enrichment" {
  event_source_arn                   = aws_sqs_queue.enrichment.arn
  function_name                      = aws_lambda_function.enrichment.arn
  batch_size                         = 5
  function_response_types            = ["ReportBatchItemFailures"]
  maximum_batching_window_in_seconds = 5
  enabled                            = true
}
