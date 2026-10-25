variable "env_prefix" {
  type        = string
  description = "Environment prefix used for resource naming (e.g., dev, staging, prod)"
}

variable "vpc_id" {
  type        = string
  description = "The ID of the VPC where web servers will be deployed"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "A list of private subnet IDs where EC2 instances will be launched"
}

variable "alb_security_group_id" {
  type        = string
  description = "The ID of the ALB's security group to allow ingress from"
}

variable "target_group_arn" {
  type        = string
  description = "The ARN of the ALB target group for ASG registration"
}

variable "iam_instance_profile_name" {
  type        = string
  description = "The name of the IAM instance profile to attach to the EC2 instances"
}

variable "image_name" {
  type        = string
  description = "AMI search pattern (e.g., ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*)"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type for web server nodes"
}

variable "public_key_content" {
  type        = string
  description = "Optional raw SSH public key content. Leave empty to rely solely on AWS SSM"
  default     = ""
}

variable "user_data_path" {
  type        = string
  description = "Path to the initialization shell script executed at instance launch"
  default     = "entry-script.sh"
}

variable "root_volume_size" {
  type        = number
  description = "Root EBS volume size in GB"
  default     = 20
}

variable "desired_capacity" {
  type        = number
  description = "Target number of active instances in the ASG"
}

variable "min_size" {
  type        = number
  description = "Minimum number of instances in the ASG"
}

variable "max_size" {
  type        = number
  description = "Maximum number of instances in the ASG"
}

variable "tags" {
  type        = map(string)
  description = "A map of tags to assign to all module resources"
  default     = {}
}
variable "app_archive_url" {
  type        = string
  description = "HTTPS URL of the application archive downloaded by the instance bootstrap script."
  validation {
    condition     = startswith(var.app_archive_url, "https://")
    error_message = "app_archive_url must use HTTPS."
  }
}

variable "cloudwatch_agent_parameter_name" {
  type        = string
  description = "SSM parameter name used to load the CloudWatch Agent configuration."
  validation {
    condition     = startswith(var.cloudwatch_agent_parameter_name, "/")
    error_message = "cloudwatch_agent_parameter_name must begin with '/'."
  }
}
