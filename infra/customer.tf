resource "aws_sqs_queue" "customer_digest_dlq" {
  name                      = "${local.name_prefix}-customer-digest-dlq"
  message_retention_seconds = 1209600
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "customer_digest" {
  name                       = "${local.name_prefix}-customer-digest"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.customer_digest_dlq.arn
    maxReceiveCount     = 3
  })
  tags = local.common_tags
}

resource "aws_cloudwatch_log_group" "customer_digest_sender" {
  name              = "/aws/lambda/${local.name_prefix}-customer-digest-sender"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "customer_digest_sender" {
  name               = "${local.name_prefix}-customer-digest-sender"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "customer_digest_sender_logs" {
  role       = aws_iam_role.customer_digest_sender.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "customer_digest_sender" {
  name = "${local.name_prefix}-customer-digest-sender"
  role = aws_iam_role.customer_digest_sender.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.customer_digest.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "arn:aws:ses:${var.aws_region}:${data.aws_caller_identity.current.account_id}:identity/${var.caresignal_email_from != "" ? var.caresignal_email_from : "disabled.invalid"}"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.backend.arn
      }
    ]
  })
}

resource "aws_lambda_function" "customer_digest_sender" {
  function_name    = "${local.name_prefix}-customer-digest-sender"
  role             = aws_iam_role.customer_digest_sender.arn
  runtime          = "python3.12"
  handler          = "app.customer_digest.sender_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 30
  memory_size      = 192

  environment {
    variables = {
      CARESIGNAL_EMAIL_FROM = var.caresignal_email_from
      BACKEND_FUNCTION_NAME = aws_lambda_function.backend.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.customer_digest_sender]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "customer_digest" {
  event_source_arn        = aws_sqs_queue.customer_digest.arn
  function_name           = aws_lambda_function.customer_digest_sender.arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
  enabled                 = var.caresignal_email_from != ""
}

resource "aws_cloudwatch_event_rule" "customer_weekly_digest" {
  name                = "${local.name_prefix}-customer-weekly-digest"
  description         = "Prepare bounded CareSignal customer weekly digests"
  schedule_expression = "cron(0 8 ? * MON *)"
  state               = var.caresignal_email_from != "" ? "ENABLED" : "DISABLED"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "customer_weekly_digest" {
  rule      = aws_cloudwatch_event_rule.customer_weekly_digest.name
  target_id = "CareSignalCustomerDigest"
  arn       = aws_lambda_function.backend.arn
  input     = jsonencode({ operation = "customer_weekly_digest" })
}

resource "aws_lambda_permission" "customer_weekly_digest" {
  statement_id  = "AllowCareSignalWeeklyDigest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.customer_weekly_digest.arn
}
