output "asg_name" {
  description = "The dynamically generated name of the Auto Scaling Group"
  value       = aws_autoscaling_group.web_asg.name
}

output "asg_id" {
  description = "The ID of the Auto Scaling Group"
  value       = aws_autoscaling_group.web_asg.id
}

output "asg_arn" {
  description = "The ARN of the Auto Scaling Group"
  value       = aws_autoscaling_group.web_asg.arn
}

output "launch_template_id" {
  description = "The ID of the launch template used by the Auto Scaling Group"
  value       = aws_launch_template.web_server_lt.id
}

output "launch_template_latest_version" {
  description = "The latest version number of the launch template"
  value       = aws_launch_template.web_server_lt.latest_version
}

output "ec2_security_group_id" {
  description = "The security group ID assigned to the web server instances"
  value       = aws_security_group.ec2_sg.id
}

output "ec2_security_group_name" {
  description = "The security group name assigned to the web server instances"
  value       = aws_security_group.ec2_sg.name
}