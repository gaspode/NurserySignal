# Phase D2 deploys disabled first. The EventBridge rule is enabled only after
# the production dry-run confirms there are no unexpected selectable records.
resource "aws_cloudwatch_event_rule" "care_withdrawal" {
  name                = "${local.name_prefix}-care-withdrawal"
  description         = "Bounded CareProspect automatic withdrawal coordinator"
  schedule_expression = "rate(6 hours)"
  state               = "ENABLED"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "care_withdrawal" {
  rule      = aws_cloudwatch_event_rule.care_withdrawal.name
  target_id = "care-withdrawal"
  arn       = aws_lambda_function.backend.arn
  input = jsonencode({
    operation = "care_withdrawal_coordinator"
    max_due   = 10
  })
}

resource "aws_lambda_permission" "care_withdrawal" {
  statement_id  = "AllowCareWithdrawalEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.care_withdrawal.arn
}
