data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name_prefix}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "lambda_application" {
  name = "${local.name_prefix}-lambda-application"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.raw_evidence.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.database.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.enrichment.arn
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.ai_invoke_resources
      }
    ]
  })
}

data "aws_iam_policy_document" "enrichment_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role" "enrichment" {
  name               = "${local.name_prefix}-enrichment"
  assume_role_policy = data.aws_iam_policy_document.enrichment_assume_role.json
  tags               = local.common_tags
}

resource "aws_iam_role_policy_attachment" "enrichment_vpc" {
  role       = aws_iam_role.enrichment.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "enrichment_application" {
  name = "${local.name_prefix}-enrichment-application"
  role = aws_iam_role.enrichment.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.database.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.enrichment.arn
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.ai_invoke_resources
      }
    ]
  })
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  tags            = local.common_tags
}

data "aws_iam_policy_document" "github_assume_role" {
  count = var.github_repository == "" ? 0 : 1
  statement {
    effect = "Allow"
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    actions = ["sts:AssumeRoleWithWebIdentity"]
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_oidc_subject_prefix}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  count              = var.github_repository == "" ? 0 : 1
  name               = "${local.name_prefix}-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_assume_role[0].json
  tags               = local.common_tags
}

resource "aws_iam_policy" "github_actions" {
  count = var.github_repository == "" ? 0 : 1
  name  = "${local.name_prefix}-github-actions"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "apigateway:*", "cloudformation:DescribeStacks", "cloudfront:*", "cognito-idp:*",
          "ec2:Describe*", "events:*",
          "iam:CreateRole", "iam:DeleteRole", "iam:Get*",
          "iam:List*", "iam:PassRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy", "lambda:*", "logs:*", "rds:*",
          "s3:*", "sqs:*", "sts:GetCallerIdentity"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["iam:TagRole", "iam:UntagRole"]
        Resource = [
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-enrichment",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-planning-collector",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.name_prefix}-ingestion-worker"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:CreateSecret"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "secretsmanager:Name" = "${local.name_prefix}/planning-provider"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:CreateSecret"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "secretsmanager:Name" = "${local.name_prefix}/recruitment-provider"
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:DescribeSecret", "secretsmanager:GetResourcePolicy", "secretsmanager:TagResource",
          "secretsmanager:UntagResource"
        ]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.name_prefix}/planning-provider-*",
          "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.name_prefix}/recruitment-provider-*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:GetPolicyVersion",
          "iam:SetDefaultPolicyVersion"
        ]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/${local.name_prefix}-github-actions"
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:DeleteSecret", "secretsmanager:DeleteSecretVersion", "secretsmanager:DescribeSecret",
          "secretsmanager:GetResourcePolicy", "secretsmanager:GetSecretValue", "secretsmanager:ListSecretVersionIds",
          "secretsmanager:PutSecretValue",
          "secretsmanager:RestoreSecret", "secretsmanager:TagResource", "secretsmanager:UntagResource",
          "secretsmanager:UpdateSecret"
        ]
        Resource = aws_secretsmanager_secret.database.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_actions" {
  count      = var.github_repository == "" ? 0 : 1
  role       = aws_iam_role.github_actions[0].name
  policy_arn = aws_iam_policy.github_actions[0].arn
}
