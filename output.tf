# ==============================================================================
# LOAD BALANCER OUTPUTS
# ==============================================================================

output "alb_dns_name" {
  description = "The DNS name of the Application Load Balancer"
  value       = module.alb.alb_dns_name
}

output "alb_arn" {
  description = "The ARN of the Application Load Balancer"
  value       = module.alb.alb_arn
}

output "alb_zone_id" {
  description = "Hosted zone ID of the ALB for Route 53 alias records"
  value       = module.alb.alb_zone_id
}

output "target_group_arn" {
  description = "Target group ARN used by Quality Gate verification pipelines"
  value       = module.alb.target_group_arn
}

output "website_url" {
  description = "Full HTTPS endpoint URL for the application"
  value       = "https://${var.domain_name}"
}

output "domain_name" {
  description = "Application domain name used for DNS and certificate validation"
  value       = var.domain_name
}

output "www_website_url" {
  description = "HTTPS URL for the www application alias, when enabled"
  value       = var.create_www_record ? "https://www.${var.domain_name}" : null
}

# ==============================================================================
# COMPUTE OUTPUTS
# ==============================================================================

output "asg_name" {
  description = "Name of the web server Auto Scaling Group"
  value       = module.webserver.asg_name
}

output "ec2_security_group_id" {
  description = "Security group ID assigned to EC2 compute nodes"
  value       = module.webserver.ec2_security_group_id
}

# ==============================================================================
# NETWORKING OUTPUTS
# ==============================================================================

output "vpc_id" {
  description = "The ID of the provisioned VPC"
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "List of public subnet IDs"
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "List of private subnet IDs"
  value       = module.network.private_subnet_ids
}

# ==============================================================================
# DNS & SECURITY OUTPUTS
# ==============================================================================

output "route53_zone_id" {
  description = "The ID of the Route 53 Hosted Zone"
  value       = module.dns.zone_id
}

output "route53_zone_name" {
  description = "The domain name managed by Route 53"
  value       = module.dns.zone_name
}

output "name_servers" {
  description = "Nameservers assigned to the Route 53 zone"
  value       = module.dns.name_servers
}

output "validated_certificate_arn" {
  description = "ARN of the validated ACM SSL Certificate"
  value       = aws_acm_certificate_validation.cert_validation.certificate_arn
}

# ==============================================================================
# MONITORING & OBSERVABILITY OUTPUTS
# ==============================================================================

output "cloudwatch_alarms_topic_arn" {
  description = "ARN of the SNS topic for CloudWatch alarms"
  value       = module.monitoring.cloudwatch_alarms_topic_arn
}

output "log_group_names" {
  description = "Map of created log group names"
  value       = module.logging.log_group_names
}

output "log_group_arns" {
  description = "Map of created log group ARNs"
  value       = module.logging.log_group_arns
}

output "app_alarm_arns" {
  description = "Map of created application metric alarm ARNs"
  value       = module.logging.alarm_arns
}