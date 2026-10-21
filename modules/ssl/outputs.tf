output "certificate_arn" {
  description = "Issued ACM certificate ARN"
  value       = aws_acm_certificate.cert.arn
}

output "domain_validation_options" {
  description = "Domain validation options for the ACM certificate."
  value       = aws_acm_certificate.cert.domain_validation_options
}

output "certificate_domain_name" {
  description = "The primary domain name of the certificate."
  value       = aws_acm_certificate.cert.domain_name
}


