resource "aws_dynamodb_table" "source_runs" {
  name         = "${local.name_prefix}-source-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "source_key"
  range_key    = "run_key"

  attribute {
    name = "source_key"
    type = "S"
  }

  attribute {
    name = "run_key"
    type = "S"
  }

  tags = local.common_tags
}
