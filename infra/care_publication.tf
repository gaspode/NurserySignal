# Phase C2 coordinator is deployed disabled first. It is enabled only after the
# bounded initial batch has been inspected and confirmed safe.
resource "aws_cloudwatch_event_rule" "care_publication" {
  name                = "${local.name_prefix}-care-publication"
  description         = "Bounded CareProspect automatic publication coordinator"
  schedule_expression = "rate(6 hours)"
  state               = "DISABLED"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "care_publication" {
  rule      = aws_cloudwatch_event_rule.care_publication.name
  target_id = "care-publication"
  arn       = aws_lambda_function.backend.arn
  input = jsonencode({
    operation = "care_publication_coordinator"
    max_due   = 25
  })
}

resource "aws_lambda_permission" "care_publication" {
  statement_id  = "AllowCarePublicationEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.care_publication.arn
}
