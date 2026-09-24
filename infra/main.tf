provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile == "" ? null : var.aws_profile
}

provider "random" {}

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_route_tables" "default" {
  vpc_id = data.aws_vpc.default.id
}

locals {
  name_prefix       = "${var.project_name}-${var.environment}"
  lambda_subnet_ids = sort(data.aws_subnets.default.ids)
  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

resource "random_password" "db" {
  length           = 32
  special          = false
  upper            = true
  lower            = true
  numeric          = true
  override_special = ""
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db"
  subnet_ids = local.lambda_subnet_ids
  tags       = local.common_tags
}

resource "aws_security_group" "lambda" {
  name        = "${local.name_prefix}-lambda"
  description = "Egress for NurserySignal Lambda functions"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.common_tags

  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name        = "${local.name_prefix}-db"
  description = "Database access only from NurserySignal Lambda functions"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.common_tags

  ingress {
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "secrets_endpoint" {
  name        = "${local.name_prefix}-secrets-endpoint"
  description = "Allow NurserySignal Lambdas to reach Secrets Manager"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.common_tags

  ingress {
    protocol        = "tcp"
    from_port       = 443
    to_port         = 443
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = data.aws_vpc.default.id
  service_name        = "com.amazonaws.${var.aws_region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = [local.lambda_subnet_ids[0]]
  security_group_ids  = [aws_security_group.secrets_endpoint.id]
  tags                = local.common_tags
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.default.ids
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = "*"
      Action    = ["s3:PutObject"]
      Resource  = "${aws_s3_bucket.raw_evidence.arn}/*"
    }]
  })
  tags = local.common_tags
}

resource "aws_security_group" "sqs_endpoint" {
  name        = "${local.name_prefix}-sqs-endpoint"
  description = "Allow NurserySignal Lambdas to reach SQS"
  vpc_id      = data.aws_vpc.default.id
  tags        = local.common_tags

  ingress {
    protocol        = "tcp"
    from_port       = 443
    to_port         = 443
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_endpoint" "sqs" {
  vpc_id              = data.aws_vpc.default.id
  service_name        = "com.amazonaws.${var.aws_region}.sqs"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.lambda_subnet_ids
  security_group_ids  = [aws_security_group.sqs_endpoint.id]
  tags                = local.common_tags
}

resource "aws_db_instance" "main" {
  identifier                      = local.name_prefix
  engine                          = "postgres"
  instance_class                  = "db.t4g.micro"
  allocated_storage               = 20
  max_allocated_storage           = 50
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = var.db_name
  username                        = var.db_username
  password                        = random_password.db.result
  port                            = 5432
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  publicly_accessible             = false
  multi_az                        = false
  backup_retention_period         = 1
  backup_window                   = "03:00-03:30"
  maintenance_window              = "sun:04:00-sun:04:30"
  deletion_protection             = false
  skip_final_snapshot             = true
  apply_immediately               = true
  copy_tags_to_snapshot           = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  tags                            = local.common_tags
}

resource "aws_secretsmanager_secret" "database" {
  name                    = "${local.name_prefix}/database"
  description             = "Runtime PostgreSQL connection details for NurserySignal"
  recovery_window_in_days = 7
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db.result
    host     = aws_db_instance.main.address
    port     = aws_db_instance.main.port
    dbname   = var.db_name
  })
}

resource "aws_s3_bucket" "raw_evidence" {
  bucket = "${local.name_prefix}-raw-evidence-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = true
  }
  tags = local.common_tags
}

resource "aws_s3_bucket_versioning" "raw_evidence" {
  bucket = aws_s3_bucket.raw_evidence.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_evidence" {
  bucket = aws_s3_bucket.raw_evidence.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "raw_evidence" {
  bucket                  = aws_s3_bucket.raw_evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "raw_evidence" {
  bucket = aws_s3_bucket.raw_evidence.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_evidence" {
  bucket = aws_s3_bucket.raw_evidence.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  rule {
    id     = "transition-old-evidence"
    status = "Enabled"
    filter { prefix = "" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
  }
}

resource "aws_sqs_queue" "ingestion_dlq" {
  name                      = "${local.name_prefix}-ingestion-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "ingestion" {
  name                       = "${local.name_prefix}-ingestion"
  visibility_timeout_seconds = 180
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.common_tags
}

resource "aws_sqs_queue" "enrichment_dlq" {
  name                      = "${local.name_prefix}-enrichment-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "enrichment" {
  name                       = "${local.name_prefix}-enrichment"
  visibility_timeout_seconds = 180
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.enrichment_dlq.arn
    maxReceiveCount     = 5
  })
  tags = local.common_tags
}

resource "aws_cloudwatch_event_rule" "collector_schedule" {
  name                = "${local.name_prefix}-collector-schedule"
  description         = "Daily bounded Plota planning collector; uses a two-day overlapping window for safe idempotent collection"
  schedule_expression = "rate(1 day)"
  state               = "ENABLED"
  tags                = local.common_tags
}
