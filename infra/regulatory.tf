resource "aws_secretsmanager_secret" "companies_house" {
  name                    = "${local.name_prefix}/companies-house"
  description             = "Companies House Public Data API key for bounded organisation enrichment"
  recovery_window_in_days = 7
  tags                    = local.common_tags
  depends_on              = [aws_iam_policy.github_actions]
}

resource "aws_cloudwatch_log_group" "ofsted_collector" {
  name              = "/aws/lambda/${local.name_prefix}-ofsted-collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "ofsted_collector" {
  name               = "${local.name_prefix}-ofsted-collector"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "ofsted_collector_logs" {
  role       = aws_iam_role.ofsted_collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ofsted_collector_application" {
  name = "${local.name_prefix}-ofsted-collector-application"
  role = aws_iam_role.ofsted_collector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.ingestion.arn },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.source_runs.arn },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.ofsted_manual_runs.arn
      }
    ]
  })
}

resource "aws_lambda_function" "ofsted_collector" {
  function_name    = "${local.name_prefix}-ofsted-collector"
  role             = aws_iam_role.ofsted_collector.arn
  runtime          = "python3.12"
  handler          = "app.ofsted_collector.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 120
  memory_size      = 512
  ephemeral_storage { size = 512 }
  environment {
    variables = {
      APP_ENV                = var.environment
      SERVICE_NAME           = "${local.name_prefix}-ofsted-collector"
      INGESTION_QUEUE_URL    = aws_sqs_queue.ingestion.url
      SOURCE_RUNS_TABLE_NAME = aws_dynamodb_table.source_runs.name
      OFSTED_DATA_URL        = "https://assets.publishing.service.gov.uk/media/697345cb51bd707cb10ed934/Inspection_and_regulation_of_childrens_social_care_and_supported_accommodation_providers_2025.ods"
    }
  }
  depends_on = [aws_cloudwatch_log_group.ofsted_collector]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "ofsted_manual_runs" {
  event_source_arn = aws_sqs_queue.ofsted_manual_runs.arn
  function_name    = aws_lambda_function.ofsted_collector.arn
  batch_size       = 1
  enabled          = true
  depends_on       = [aws_iam_role_policy.ofsted_collector_application]
}

resource "aws_cloudwatch_log_group" "companies_house_collector" {
  name              = "/aws/lambda/${local.name_prefix}-companies-house-collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "companies_house_collector" {
  name               = "${local.name_prefix}-companies-house-collector"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "companies_house_collector_logs" {
  role       = aws_iam_role.companies_house_collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "companies_house_collector_application" {
  name = "${local.name_prefix}-companies-house-collector-application"
  role = aws_iam_role.companies_house_collector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.companies_house.arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.ingestion.arn },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.source_runs.arn },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.companies_house_manual_runs.arn
      }
    ]
  })
}

resource "aws_lambda_function" "companies_house_collector" {
  function_name    = "${local.name_prefix}-companies-house-collector"
  role             = aws_iam_role.companies_house_collector.arn
  runtime          = "python3.12"
  handler          = "app.companies_house_collector.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256
  environment {
    variables = {
      APP_ENV                    = var.environment
      SERVICE_NAME               = "${local.name_prefix}-companies-house-collector"
      INGESTION_QUEUE_URL        = aws_sqs_queue.ingestion.url
      SOURCE_RUNS_TABLE_NAME     = aws_dynamodb_table.source_runs.name
      COMPANIES_HOUSE_SECRET_ARN = aws_secretsmanager_secret.companies_house.arn
      COMPANIES_HOUSE_BASE_URL   = "https://api.company-information.service.gov.uk"
    }
  }
  depends_on = [aws_cloudwatch_log_group.companies_house_collector]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "companies_house_manual_runs" {
  event_source_arn = aws_sqs_queue.companies_house_manual_runs.arn
  function_name    = aws_lambda_function.companies_house_collector.arn
  batch_size       = 1
  enabled          = true
  depends_on       = [aws_iam_role_policy.companies_house_collector_application]
}
