variable "aws_region" {
  type        = string
  description = "AWS region for infrastructure deployment"
  default     = "us-east-1"
}

variable "env_prefix" {
  type        = string
  description = "Environment prefix used for resource naming (e.g., dev, staging, prod)"
}

variable "vpc_cidr_block" {
  type        = string
  description = "Base IPv4 CIDR block for the primary VPC"
}

variable "az_count" {
  type        = number
  description = "Number of Availability Zones to deploy subnets into"
  default     = 2
}

variable "my_ip" {
  type        = string
  description = "Administrator IP address for restricted access (CIDR format)"
}

variable "domain_name" {
  type        = string
  description = "The registered domain name for DNS routing and ACM SSL certification"
}

variable "create_www_record" {
  type        = bool
  description = "Whether to create a www alias record for the application."
  default     = true
}

variable "image_name" {
  type        = string
  description = "AMI search pattern for EC2 instance launch template"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type for Auto Scaling Group nodes"
}

variable "app_archive_url" {
  type        = string
  description = "HTTPS URL of the application source archive consumed by EC2 bootstrap"
}

variable "cloudwatch_agent_parameter_name" {
  type        = string
  description = "SSM Parameter Store name containing the CloudWatch Agent configuration"
}

variable "public_key_content" {
  type        = string
  description = "Raw public SSH key content for direct host access. Leave empty if using SSM"
}

variable "desired_capacity" {
  type        = number
  description = "Target number of instances in the Auto Scaling Group"
  default     = 2
}

variable "min_size" {
  type        = number
  description = "Minimum number of instances in the Auto Scaling Group"
  default     = 2
}

variable "max_size" {
  type        = number
  description = "Maximum number of instances in the Auto Scaling Group"
  default     = 4
}

# variable "slack_webhook_secret_arn" {
#   type        = string
#   description = "ARN of a Secrets Manager secret containing the Slack incoming webhook URL"
#   sensitive   = true
#   validation {
#     condition     = can(regex("^arn:[^:]+:secretsmanager:[^:]+:[0-9]{12}:secret:.+", var.slack_webhook_secret_arn))
#     error_message = "slack_webhook_secret_arn must be a Secrets Manager secret ARN."
#   }
# }
variable "slack_webhook_url" {
  type        = string
  description = "Slack Webhook URL for CloudWatch incident alerting and remediation events"
  sensitive   = true
}

variable "alert_email" {
  type        = string
  description = "Email address for SNS CloudWatch alarm notifications"
  default     = ""
  sensitive   = true
}

# ==============================================================================
# LOGGING & OBSERVABILITY VARIABLES
# ==============================================================================

variable "default_log_retention_days" {
  type        = number
  description = "Default CloudWatch Log Group retention in days"
  default     = 30
}

variable "log_groups" {
  type = map(object({
    retention_in_days = optional(number)
    kms_key_id        = optional(string)
  }))
  description = "Map of CloudWatch log group definitions"
  default     = {}
}

variable "metric_filters" {
  type = map(object({
    log_group_key    = string
    pattern          = string
    metric_name      = string
    metric_namespace = string
    metric_value     = optional(string, "1")
    default_value    = optional(string, "0")
  }))
  description = "Map of log metric filters to convert logs into custom CloudWatch metrics"
  default     = {}
}

variable "app_alarms" {
  type = map(object({
    metric_name         = string
    metric_namespace    = string
    threshold           = number
    is_custom_metric    = optional(bool, true)
    comparison_operator = optional(string, "GreaterThanOrEqualToThreshold")
    evaluation_periods  = optional(number, 1)
    period              = optional(number, 60)
    statistic           = optional(string, "Sum")
    description         = optional(string)
    treat_missing_data  = optional(string, "notBreaching")
    dimensions          = optional(map(string), {})
  }))
  description = "Map of custom CloudWatch application alarms"
  default     = {}
}

variable "tags" {
  type        = map(string)
  description = "Resource tags applied across all deployed modules"
  default     = {}
}