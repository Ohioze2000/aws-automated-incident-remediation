variable "env_prefix" {
  type        = string
  description = "Environment prefix used for resource naming (e.g., dev, staging, prod)"
}

variable "default_log_retention_days" {
  type        = number
  description = "Default log retention period in days for CloudWatch Log Groups"
  default     = 30
}

variable "kms_key_id" {
  type        = string
  description = "Optional KMS Key ARN to encrypt CloudWatch log groups at rest"
  default     = null
}

variable "log_groups" {
  type = map(object({
    retention_in_days = optional(number)
    kms_key_id        = optional(string)
  }))
  description = "Map of log group definitions where key is the logical application component name"
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
  description = "Map of custom CloudWatch log metric filters to parse error logs or custom events"
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
  description = "Map of CloudWatch metric alarms monitoring application or log metrics"
  default     = {}
}

variable "alarm_sns_topic_arns" {
  type        = list(string)
  description = "List of SNS Topic ARNs to trigger when an alarm enters ALARM or OK state"
  default     = []
}

variable "tags" {
  type        = map(string)
  description = "A map of tags to assign to all module resources"
  default     = {}
}