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
      APP_ENV       = var.environment
      SERVICE_NAME  = "${local.name_prefix}-api"
      DB_SECRET_ARN = aws_secretsmanager_secret.database.arn
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
