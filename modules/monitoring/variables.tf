variable "env_prefix" {
  type        = string
  description = "Environment prefix used for naming resources (e.g., dev, staging, prod)"
}

variable "asg_name" {
  type        = string
  description = "The name of the Auto Scaling Group to monitor and remediate"
}

variable "target_group_arn" {
  type        = string
  description = "The ARN of the ALB Target Group to query for unhealthy application instances"
}

variable "target_group_dimension" {
  type        = string
  description = "CloudWatch TargetGroup dimension in targetgroup/name/id format"
}

variable "load_balancer_dimension" {
  type        = string
  description = "CloudWatch LoadBalancer dimension in app/name/id format"
}

variable "memory_alarm_threshold" {
  type        = number
  description = "Memory used percentage that triggers the ASG memory alarm"
  default     = 85
  validation {
    condition     = var.memory_alarm_threshold > 0 && var.memory_alarm_threshold <= 100
    error_message = "memory_alarm_threshold must be between 0 and 100."
  }
}

variable "max_remediation_attempts" {
  type        = number
  description = "Maximum automatic remediation attempts per incident"
  default     = 1
  validation {
    condition     = var.max_remediation_attempts >= 1 && var.max_remediation_attempts <= 3
    error_message = "max_remediation_attempts must be between 1 and 3."
  }
}

variable "verification_timeout_seconds" {
  type        = number
  description = "Maximum time spent checking the outcome of a recovery action"
  default     = 180
}

variable "incident_ttl_days" {
  type        = number
  description = "Retention window for incident records before DynamoDB TTL cleanup"
  default     = 90
}

variable "slack_webhook_secret_arn" {
  type        = string
  description = "Secrets Manager ARN holding the Slack incoming webhook URL"
  sensitive   = true
}

variable "alert_email" {
  type        = string
  description = "Optional email address to subscribe to SNS alarm notifications"
  default     = ""
}

variable "sns_kms_key_id" {
  type        = string
  description = "KMS Key ID or ARN to encrypt the SNS Topic at rest"
  default     = null
}

# --- CPU Alarm Variables ---
variable "cpu_alarm_threshold" {
  type        = number
  description = "Average CPU threshold percent to trigger alarm"
  default     = 80
}

variable "cpu_evaluation_periods" {
  type        = number
  description = "The number of periods over which data is compared against the threshold"
  default     = 2
}

variable "cpu_datapoints_to_alarm" {
  type        = number
  description = "The number of datapoints within the evaluation period that must be breaching to trigger the alarm"
  default     = 2
}

variable "cpu_alarm_period" {
  type        = number
  description = "The period in seconds over which the specified statistic is applied"
  default     = 300
}

# --- Disk Alarm Variables ---
variable "enable_disk_alarm" {
  type        = bool
  description = "Whether to deploy the high disk usage alarm"
  default     = true
}

variable "disk_alarm_threshold" {
  type        = number
  description = "Root disk usage threshold percentage to trigger alarm"
  default     = 70
}

variable "disk_evaluation_periods" {
  type        = number
  description = "Evaluation periods for disk alarm"
  default     = 1
}

variable "disk_alarm_period" {
  type        = number
  description = "Period in seconds for disk alarm evaluation"
  default     = 120
}

# --- Log Error Alarm Variables ---
variable "enable_log_error_alarm" {
  type        = bool
  description = "Whether to enable application log error monitoring"
  default     = true
}

variable "app_log_group_name" {
  type        = string
  description = "Name of the CloudWatch log group storing application logs"
  default     = "/ec2/app-logs"
}

variable "log_error_pattern" {
  type        = string
  description = "Metric filter pattern to search for error terms in logs"
  default     = "?ERROR ?Error ?error ?500 ?EXCEPTION ?Exception"
}

variable "log_error_threshold" {
  type        = number
  description = "Number of log error occurrences in period to trigger alarm"
  default     = 5
}

variable "log_error_alarm_period" {
  type        = number
  description = "Evaluation window in seconds for error log frequency"
  default     = 60
}

# --- General Variables ---
variable "log_retention_in_days" {
  type        = number
  description = "CloudWatch Log Group retention period in days for the remediation Lambda"
  default     = 14
}

variable "disk_fstype" {
  type        = string
  description = "Filesystem type of the root volume (e.g., ext4 for Ubuntu/Debian, xfs for Amazon Linux)"
  default     = "ext4"
}

variable "tags" {
  type        = map(string)
  description = "A mapping of tags to assign to all module resources"
  default     = {}
}
variable "min_healthy_targets" {
  type        = number
  description = "Minimum healthy ALB targets that must remain before an unhealthy target may be replaced"
  default     = 1
  validation {
    condition     = var.min_healthy_targets >= 1
    error_message = "min_healthy_targets must be at least 1."
  }
}
