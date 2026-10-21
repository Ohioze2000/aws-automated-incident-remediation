output "alb_id" {
  description = "The ID of the Application Load Balancer"
  value       = aws_lb.app_alb.id
}

output "alb_arn" {
  description = "The ARN of the Application Load Balancer"
  value       = aws_lb.app_alb.arn
}

output "alb_dns_name" {
  description = "The DNS name of the Application Load Balancer"
  value       = aws_lb.app_alb.dns_name
}

output "alb_zone_id" {
  description = "The AWS-managed hosted zone ID for the ALB (used for Route 53 alias records)"
  value       = aws_lb.app_alb.zone_id
}

output "alb_security_group_id" {
  description = "The Security Group ID assigned to the ALB"
  value       = aws_security_group.alb_sg.id
}

output "target_group_arn" {
  description = "The ARN of the Target Group for Auto Scaling Group attachment"
  value       = aws_lb_target_group.app_tg.arn
}

output "target_group_name" {
  description = "The name of the Target Group"
  value       = aws_lb_target_group.app_tg.name
}