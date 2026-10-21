resource "aws_acm_certificate" "cert" {
  domain_name               = var.domain_name
  validation_method         = "DNS"
  subject_alternative_names = var.subject_alternative_names

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-acm-cert-${var.domain_name}"
    }
  )

  lifecycle {
    create_before_destroy = true
  }
}