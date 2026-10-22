variable "aws_region" {
  type        = string
  description = "AWS region for infrastructure deployment"
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
}

variable "my_ip" {
  type        = string
  description = "Administrator IP address for restricted access (CIDR format)"
}

variable "create_www_record" {
  type        = bool
  description = "Whether to create a www DNS alias for the application."
  default     = true
}

variable "domain_name" {
  type        = string
  description = "The registered domain name for DNS routing and ACM SSL certification"
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
  description = "HTTPS URL of the application archive downloaded by EC2 bootstrap."
  validation {
    condition     = startswith(var.app_archive_url, "https://")
    error_message = "app_archive_url must use HTTPS."
  }
}

variable "cloudwatch_agent_parameter_name" {
  type        = string
  description = "SSM parameter name containing the CloudWatch Agent configuration."
  validation {
    condition     = startswith(var.cloudwatch_agent_parameter_name, "/")
    error_message = "cloudwatch_agent_parameter_name must begin with '/'."
  }
}

variable "public_key_content" {
  type        = string
  description = "Raw public SSH key content for direct host access. Leave empty if using SSM"
}

variable "desired_capacity" {
  type        = number
  description = "Target number of instances in the Auto Scaling Group"
}

variable "min_size" {
  type        = number
  description = "Minimum number of instances in the Auto Scaling Group"
}

variable "max_size" {
  type        = number
  description = "Maximum number of instances in the Auto Scaling Group"
}

variable "slack_webhook_url" {
  type        = string
  description = "Slack incoming webhook URL. Set as a sensitive HCP Terraform workspace variable; never commit a real value."
  sensitive   = true
  validation {
    condition     = startswith(var.slack_webhook_url, "https://hooks.slack.com/")
    error_message = "slack_webhook_url must be a Slack incoming webhook URL beginning with https://hooks.slack.com/."
  }
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