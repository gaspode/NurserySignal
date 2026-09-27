resource "aws_cloudwatch_log_group" "procurement_collector" {
  name              = "/aws/lambda/${local.name_prefix}-procurement-collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "procurement_collector" {
  name               = "${local.name_prefix}-procurement-collector"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "procurement_collector_logs" {
  role       = aws_iam_role.procurement_collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "procurement_collector_application" {
  name = "${local.name_prefix}-procurement-collector-application"
  role = aws_iam_role.procurement_collector.id
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
        Resource = aws_sqs_queue.procurement_manual_runs.arn
      }
    ]
  })
}

resource "aws_lambda_function" "procurement_collector" {
  function_name    = "${local.name_prefix}-procurement-collector"
  role             = aws_iam_role.procurement_collector.arn
  runtime          = "python3.12"
  handler          = "app.procurement_collector.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 300
  memory_size      = 256
  environment {
    variables = {
      APP_ENV                = var.environment
      SERVICE_NAME           = "${local.name_prefix}-procurement-collector"
      INGESTION_QUEUE_URL    = aws_sqs_queue.ingestion.url
      SOURCE_RUNS_TABLE_NAME = aws_dynamodb_table.source_runs.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.procurement_collector]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "procurement_manual_runs" {
  event_source_arn = aws_sqs_queue.procurement_manual_runs.arn
  function_name    = aws_lambda_function.procurement_collector.arn
  batch_size       = 1
  enabled          = true
  depends_on       = [aws_iam_role_policy.procurement_collector_application]
}
