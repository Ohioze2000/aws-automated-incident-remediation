output "iam_role_name" {
  description = "The name of the IAM role created for EC2 instances"
  value       = aws_iam_role.ec2_ssm_role.name
}

output "iam_role_arn" {
  description = "The ARN of the IAM role created for EC2 instances"
  value       = aws_iam_role.ec2_ssm_role.arn
}

output "iam_instance_profile_name" {
  description = "The name of the IAM Instance Profile created for EC2 instances"
  value       = aws_iam_instance_profile.ec2_ssm_profile.name
}

output "iam_instance_profile_arn" {
  description = "The ARN of the IAM Instance Profile created for EC2 instances"
  value       = aws_iam_instance_profile.ec2_ssm_profile.arn
}