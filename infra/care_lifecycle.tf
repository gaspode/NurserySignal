# Phase B3 coordinator: deployed disabled first, then enabled only after the
# durable cohort and deterministic initial stagger have been verified.
resource "aws_cloudwatch_event_rule" "care_lifecycle_refresh" {
  name                = "${local.name_prefix}-care-lifecycle-refresh"
  description         = "Bounded CareProspect Planning lifecycle refresh coordinator"
  schedule_expression = "rate(6 hours)"
  state               = "DISABLED"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "care_lifecycle_refresh" {
  rule      = aws_cloudwatch_event_rule.care_lifecycle_refresh.name
  target_id = "care-lifecycle-refresh"
  arn       = aws_lambda_function.backend.arn
  input = jsonencode({
    operation = "care_lifecycle_refresh_coordinator"
    preview   = false
    max_due   = 15
  })
}

resource "aws_lambda_permission" "care_lifecycle_refresh" {
  statement_id  = "AllowCareLifecycleRefreshEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.care_lifecycle_refresh.arn
}
