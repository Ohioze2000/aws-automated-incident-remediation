output "cloudwatch_alarms_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarm notifications"
  value       = aws_sns_topic.cloudwatch_alarms_topic.arn
}
output "dispatcher_lambda_arn" {
  description = "Incident dispatcher Lambda ARN"
  value       = aws_lambda_function.incident_dispatcher.arn
}
output "remediation_worker_lambda_arn" {
  description = "Bounded remediation worker Lambda ARN"
  value       = aws_lambda_function.remediation_worker.arn
}
output "incident_table_name" {
  description = "DynamoDB table that stores incident lifecycle records"
  value       = aws_dynamodb_table.incidents.name
}
output "memory_alarm_arn" {
  description = "ARN of the ASG memory alarm"
  value       = aws_cloudwatch_metric_alarm.high_memory_alarm.arn
}
output "unhealthy_alb_alarm_arn" {
  description = "ARN of the unhealthy ALB target alarm"
  value       = aws_cloudwatch_metric_alarm.unhealthy_alb_alarm.arn
}
output "high_disk_alarm_arn" {
  description = "ARN of the high disk utilization alarm, if enabled"
  value       = length(aws_cloudwatch_metric_alarm.high_disk_alarm) > 0 ? aws_cloudwatch_metric_alarm.high_disk_alarm[0].arn : null
}
