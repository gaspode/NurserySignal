resource "aws_secretsmanager_secret" "planning_provider" {
  name                    = "${local.name_prefix}/planning-provider"
  description             = "Planning provider API key for the NurserySignal collector"
  recovery_window_in_days = 7
  tags                    = local.common_tags
  depends_on              = [aws_iam_policy.github_actions]
}

resource "aws_cloudwatch_log_group" "planning_collector" {
  name              = "/aws/lambda/${local.name_prefix}-planning-collector"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "planning_collector" {
  name               = "${local.name_prefix}-planning-collector"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
  depends_on         = [aws_iam_policy.github_actions]
}

resource "aws_iam_role_policy_attachment" "planning_collector_logs" {
  role       = aws_iam_role.planning_collector.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "planning_collector_application" {
  name = "${local.name_prefix}-planning-collector-application"
  role = aws_iam_role.planning_collector.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.planning_provider.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.ingestion.arn
      }
    ]
  })
}

resource "aws_lambda_function" "planning_collector" {
  function_name    = "${local.name_prefix}-planning-collector"
  role             = aws_iam_role.planning_collector.arn
  runtime          = "python3.12"
  handler          = "app.collector.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      APP_ENV                      = var.environment
      SERVICE_NAME                 = "${local.name_prefix}-planning-collector"
      INGESTION_QUEUE_URL          = aws_sqs_queue.ingestion.url
      PLANNING_PROVIDER_SECRET_ARN = aws_secretsmanager_secret.planning_provider.arn
      PLANNING_PROVIDER_BASE_URL   = "https://api.plota.co.uk/v1"
    }
  }

  depends_on = [aws_cloudwatch_log_group.planning_collector]
  tags       = local.common_tags
}

resource "aws_cloudwatch_event_target" "planning_collector" {
  rule      = aws_cloudwatch_event_rule.collector_schedule.name
  target_id = "planning-collector"
  arn       = aws_lambda_function.planning_collector.arn
  input = jsonencode({
    source        = "scheduled"
    lookback_days = 2
    max_records   = 100
    page_size     = 25
  })
}

resource "aws_lambda_permission" "planning_collector_schedule" {
  statement_id  = "AllowCollectorSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.planning_collector.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.collector_schedule.arn
}

resource "aws_cloudwatch_log_group" "ingestion_worker" {
  name              = "/aws/lambda/${local.name_prefix}-ingestion-worker"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "ingestion_worker" {
  name               = "${local.name_prefix}-ingestion-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
  depends_on         = [aws_iam_policy.github_actions]
}

resource "aws_iam_role_policy_attachment" "ingestion_worker_vpc" {
  role       = aws_iam_role.ingestion_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "ingestion_worker_application" {
  name = "${local.name_prefix}-ingestion-worker-application"
  role = aws_iam_role.ingestion_worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.raw_evidence.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.database.arn
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.ingestion.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.enrichment.arn
      }
    ]
  })
}

resource "aws_lambda_function" "ingestion_worker" {
  function_name    = "${local.name_prefix}-ingestion-worker"
  role             = aws_iam_role.ingestion_worker.arn
  runtime          = "python3.12"
  handler          = "app.ingestion_worker.handler"
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
      APP_ENV              = var.environment
      SERVICE_NAME         = "${local.name_prefix}-ingestion-worker"
      DB_SECRET_ARN        = aws_secretsmanager_secret.database.arn
      EVIDENCE_BUCKET      = aws_s3_bucket.raw_evidence.bucket
      ENRICHMENT_QUEUE_URL = aws_sqs_queue.enrichment.url
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingestion_worker]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "ingestion" {
  event_source_arn                   = aws_sqs_queue.ingestion.arn
  function_name                      = aws_lambda_function.ingestion_worker.arn
  batch_size                         = 5
  function_response_types            = ["ReportBatchItemFailures"]
  maximum_batching_window_in_seconds = 5
  enabled                            = true
}
