output "zone_id" {
  description = "The Route 53 Hosted Zone ID."
  value       = data.aws_route53_zone.primary.zone_id
}

output "zone_name" {
  description = "The verified zone domain name."
  value       = data.aws_route53_zone.primary.name
}

output "name_servers" {
  description = "The name servers assigned to this hosted zone."
  value       = data.aws_route53_zone.primary.name_servers
}

output "root_url" {
  description = "The FQDN of the apex domain."
  value       = aws_route53_record.root_a.fqdn
}

output "website_url" {
  description = "The FQDN of the www subdomain record (if enabled)."
  value       = var.create_www_record ? aws_route53_record.www_a[0].fqdn : aws_route53_record.root_a.fqdn
}