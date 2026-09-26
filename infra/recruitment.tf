resource "aws_secretsmanager_secret" "recruitment_provider" {
  name                    = "${local.name_prefix}/recruitment-provider"
  description             = "Find an Apprenticeship Display Advert API key for the bounded recruitment collector"
  recovery_window_in_days = 7
  tags                    = local.common_tags
  depends_on              = [aws_iam_policy.github_actions]
}

resource "aws_cloudwatch_log_group" "recruitment_collector" {
  name              = "/aws/lambda/${local.name_prefix}-recruitment-collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "recruitment_collector" {
  name               = "${local.name_prefix}-recruitment-collector"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  depends_on         = [aws_iam_policy.github_actions]
}

resource "aws_iam_role_policy_attachment" "recruitment_collector_logs" {
  role       = aws_iam_role.recruitment_collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "recruitment_collector_application" {
  name = "${local.name_prefix}-recruitment-collector-application"
  role = aws_iam_role.recruitment_collector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.recruitment_provider.arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.ingestion.arn },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.source_runs.arn }
    ]
  })
}

resource "aws_lambda_function" "recruitment_collector" {
  function_name    = "${local.name_prefix}-recruitment-collector"
  role             = aws_iam_role.recruitment_collector.arn
  runtime          = "python3.12"
  handler          = "app.recruitment_collector.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256
  environment {
    variables = {
      APP_ENV                         = var.environment
      SERVICE_NAME                    = "${local.name_prefix}-recruitment-collector"
      INGESTION_QUEUE_URL             = aws_sqs_queue.ingestion.url
      RECRUITMENT_PROVIDER_SECRET_ARN = aws_secretsmanager_secret.recruitment_provider.arn
      RECRUITMENT_PROVIDER_BASE_URL   = "https://api.apprenticeships.education.gov.uk/vacancies"
      SOURCE_RUNS_TABLE_NAME          = aws_dynamodb_table.source_runs.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.recruitment_collector]
  tags       = local.common_tags
}

resource "aws_cloudwatch_event_rule" "recruitment_schedule" {
  name                = "${local.name_prefix}-recruitment-schedule"
  description         = "Bounded GOV.UK apprenticeship recruitment collector"
  schedule_expression = "rate(1 day)"
  state               = "ENABLED"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "recruitment_collector" {
  rule      = aws_cloudwatch_event_rule.recruitment_schedule.name
  target_id = "recruitment-collector"
  arn       = aws_lambda_function.recruitment_collector.arn
  input     = jsonencode({ source = "scheduled", posted_since_days = 7, max_records = 50, page_size = 25 })
}

resource "aws_lambda_permission" "recruitment_collector_schedule" {
  statement_id  = "AllowRecruitmentCollectorSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.recruitment_collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.recruitment_schedule.arn
}
