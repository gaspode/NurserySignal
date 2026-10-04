resource "aws_cloudwatch_log_group" "official_planning_fetcher" {
  name              = "/aws/lambda/${local.name_prefix}-official-planning-fetcher"
  retention_in_days = 30
  tags              = local.common_tags
}

# This role intentionally has only CloudWatch Logs access. In particular it has
# no VPC attachment, database secret, S3, SQS, DynamoDB or business-operation
# permissions. It can only make outbound HTTPS from the Lambda public egress.
resource "aws_iam_role" "official_planning_fetcher" {
  name               = "${local.name_prefix}-official-planning-fetcher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "official_planning_fetcher_logs" {
  role       = aws_iam_role.official_planning_fetcher.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "official_planning_fetcher" {
  function_name    = "${local.name_prefix}-official-planning-fetcher"
  role             = aws_iam_role.official_planning_fetcher.arn
  runtime          = "python3.12"
  handler          = "app.official_planning_fetcher.handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = 12
  memory_size      = 128

  # This is deliberately not VPC-attached. It is the only narrow public-web
  # execution boundary and cannot access the private database network.
  environment {
    variables = {
      OFFICIAL_PLANNING_ALLOWED_HOSTS = join(",", [
        "pa.blaby.gov.uk",
        "pa.brent.gov.uk",
        "planning.bolsover.gov.uk",
        "planning.bradford.gov.uk",
        "planning.luton.gov.uk",
        "planning.mansfield.gov.uk",
        "planning.warwickdc.gov.uk",
        "publicaccess.nottinghamcity.gov.uk",
        "publicaccess1.medway.gov.uk"
      ])
    }
  }

  depends_on = [aws_cloudwatch_log_group.official_planning_fetcher]
  tags       = local.common_tags
}
