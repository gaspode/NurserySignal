resource "aws_route53_zone" "careprospect" {
  name = "careprospect.co.uk"

  tags = merge(local.common_tags, {
    Product = "CareProspect"
  })
}
