output "log_group_arns" {
  description = "Map of log group ARNs keyed by log group key"
  value       = { for k, v in aws_cloudwatch_log_group.app_log_groups : k => v.arn }
}

output "log_group_names" {
  description = "Map of log group names keyed by log group key"
  value       = { for k, v in aws_cloudwatch_log_group.app_log_groups : k => v.name }
}

output "metric_filter_names" {
  description = "Map of generated metric filter names keyed by metric filter key"
  value       = { for k, v in aws_cloudwatch_log_metric_filter.filters : k => v.name }
}

output "alarm_arns" {
  description = "Map of created CloudWatch Metric Alarm ARNs"
  value       = { for k, v in aws_cloudwatch_metric_alarm.app_alarms : k => v.arn }
}