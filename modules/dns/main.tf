# Fetch Existing Route 53 Public Hosted Zone
data "aws_route53_zone" "primary" {
  name         = var.domain_name
  private_zone = false
}

# ------------------------------------------------------------------------------
# APEX DOMAIN (A & AAAA ALIAS RECORDS)
# ------------------------------------------------------------------------------
resource "aws_route53_record" "root_a" {
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "root_aaaa" {
  count   = var.enable_ipv6 ? 1 : 0
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = var.domain_name
  type    = "AAAA"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}

# ------------------------------------------------------------------------------
# WWW SUBDOMAIN (A & AAAA ALIAS RECORDS)
# ------------------------------------------------------------------------------
resource "aws_route53_record" "www_a" {
  count   = var.create_www_record ? 1 : 0
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "www.${var.domain_name}"
  type    = "A"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "www_aaaa" {
  count   = (var.create_www_record && var.enable_ipv6) ? 1 : 0
  zone_id = data.aws_route53_zone.primary.zone_id
  name    = "www.${var.domain_name}"
  type    = "AAAA"

  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
}