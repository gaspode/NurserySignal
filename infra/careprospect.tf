resource "aws_route53_zone" "careprospect" {
  name = "careprospect.co.uk"

  tags = merge(local.common_tags, {
    Product = "CareProspect"
  })

  depends_on = [aws_iam_policy.github_actions]
}

resource "aws_acm_certificate" "careprospect" {
  provider          = aws.us_east_1
  domain_name       = aws_route53_zone.careprospect.name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.common_tags, {
    Product = "CareProspect"
  })
}

resource "aws_route53_record" "careprospect_certificate_validation" {
  for_each = {
    for option in aws_acm_certificate.careprospect.domain_validation_options : option.domain_name => {
      name   = option.resource_record_name
      record = option.resource_record_value
      type   = option.resource_record_type
    }
  }

  zone_id = aws_route53_zone.careprospect.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 300
  records = [each.value.record]
}

resource "aws_acm_certificate_validation" "careprospect" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.careprospect.arn
  validation_record_fqdns = [for record in aws_route53_record.careprospect_certificate_validation : record.fqdn]
}

resource "aws_route53_record" "careprospect_frontend_a" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = aws_route53_zone.careprospect.name
  type    = "A"

  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = aws_cloudfront_distribution.frontend.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "careprospect_frontend_aaaa" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = aws_route53_zone.careprospect.name
  type    = "AAAA"

  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = aws_cloudfront_distribution.frontend.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_ses_domain_identity" "careprospect" {
  domain = aws_route53_zone.careprospect.name
}

resource "aws_route53_record" "careprospect_ses_verification" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = "_amazonses.${aws_route53_zone.careprospect.name}"
  type    = "TXT"
  ttl     = 300
  records = [aws_ses_domain_identity.careprospect.verification_token]
}

resource "aws_ses_domain_identity_verification" "careprospect" {
  domain     = aws_ses_domain_identity.careprospect.id
  depends_on = [aws_route53_record.careprospect_ses_verification]
}

resource "aws_ses_domain_dkim" "careprospect" {
  domain = aws_ses_domain_identity.careprospect.domain
}

resource "aws_route53_record" "careprospect_dkim" {
  count = 3

  zone_id = aws_route53_zone.careprospect.zone_id
  name    = "${aws_ses_domain_dkim.careprospect.dkim_tokens[count.index]}._domainkey.${aws_route53_zone.careprospect.name}"
  type    = "CNAME"
  ttl     = 300
  records = ["${aws_ses_domain_dkim.careprospect.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_ses_domain_mail_from" "careprospect" {
  domain                 = aws_ses_domain_identity.careprospect.domain
  mail_from_domain       = "bounce.${aws_route53_zone.careprospect.name}"
  behavior_on_mx_failure = "UseDefaultValue"
}

resource "aws_route53_record" "careprospect_mail_from_mx" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = aws_ses_domain_mail_from.careprospect.mail_from_domain
  type    = "MX"
  ttl     = 300
  records = ["10 feedback-smtp.${var.aws_region}.amazonses.com"]
}

resource "aws_route53_record" "careprospect_mail_from_spf" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = aws_ses_domain_mail_from.careprospect.mail_from_domain
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com -all"]
}

resource "aws_route53_record" "careprospect_spf" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = aws_route53_zone.careprospect.name
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com -all"]
}

resource "aws_route53_record" "careprospect_dmarc" {
  zone_id = aws_route53_zone.careprospect.zone_id
  name    = "_dmarc.${aws_route53_zone.careprospect.name}"
  type    = "TXT"
  ttl     = 300
  records = ["v=DMARC1; p=none; adkim=s; aspf=s; pct=100"]
}
