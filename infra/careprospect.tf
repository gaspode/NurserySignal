resource "aws_route53_zone" "careprospect" {
  name = "careprospect.co.uk"

  tags = merge(local.common_tags, {
    Product = "CareProspect"
  })

  depends_on = [aws_iam_policy.github_actions]
}
