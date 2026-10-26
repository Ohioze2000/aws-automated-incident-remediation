data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Production incident path: SNS -> idempotent dispatcher -> DynamoDB -> worker.
resource "aws_dynamodb_table" "incidents" {
  name         = "${var.env_prefix}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"

  attribute {
    name = "incident_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  tags = merge(var.tags, { Name = "${var.env_prefix}-incidents" })
}

resource "aws_dynamodb_table" "asg_lifecycle_events" {
  name         = "${var.env_prefix}-asg-lifecycle-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "asg_name"
  range_key    = "event_key"

  attribute {
    name = "asg_name"
    type = "S"
  }
  attribute {
    name = "event_key"
    type = "S"
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled = true
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  tags = merge(var.tags, { Name = "${var.env_prefix}-asg-lifecycle-events" })
}

resource "aws_iam_role" "dispatcher_role" {
  name = "${var.env_prefix}-incident-dispatcher-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}
resource "aws_iam_role_policy_attachment" "dispatcher_logs" {
  role       = aws_iam_role.dispatcher_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "dispatcher_policy" {
  name = "${var.env_prefix}-incident-dispatcher-policy"
  role = aws_iam_role.dispatcher_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.incidents.arn },
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = aws_lambda_function.remediation_worker.arn }
    ]
  })
}

resource "aws_iam_role" "worker_role" {
  name = "${var.env_prefix}-remediation-worker-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}
resource "aws_iam_role_policy_attachment" "worker_logs" {
  role       = aws_iam_role.worker_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "worker_policy" {
  name = "${var.env_prefix}-remediation-worker-policy"
  role = aws_iam_role.worker_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.incidents.arn },
      { Effect = "Allow", Action = ["dynamodb:Query"], Resource = aws_dynamodb_table.asg_lifecycle_events.arn },
      { Effect = "Allow", Action = ["elasticloadbalancing:DescribeTargetHealth"], Resource = "*" },
      { Effect = "Allow", Action = ["autoscaling:DescribeAutoScalingGroups", "autoscaling:TerminateInstanceInAutoScalingGroup"], Resource = "*" },
      { Effect = "Allow", Action = ["ec2:DescribeInstances"], Resource = "*" },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.worker_failure_dlq.arn },
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:${data.aws_partition.current.partition}:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.env_prefix}-incident-event-router" }
    ]
  })
}

resource "aws_sqs_queue" "worker_failure_dlq" {
  name                      = "${var.env_prefix}-remediation-worker-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_cloudwatch_log_group" "dispatcher_logs" {
  name              = "/aws/lambda/${var.env_prefix}-incident-dispatcher"
  retention_in_days = var.log_retention_in_days
  tags              = var.tags
}
resource "aws_cloudwatch_log_group" "worker_logs" {
  name              = "/aws/lambda/${var.env_prefix}-remediation-worker"
  retention_in_days = var.log_retention_in_days
  tags              = var.tags
}

resource "aws_lambda_function" "remediation_worker" {
  filename         = data.archive_file.worker_zip.output_path
  function_name    = "${var.env_prefix}-remediation-worker"
  role             = aws_iam_role.worker_role.arn
  handler          = "remediation_worker.lambda_handler"
  runtime          = "python3.12"
  timeout          = 600
  memory_size      = 256
  source_code_hash = data.archive_file.worker_zip.output_base64sha256
  environment {
    variables = {
      INCIDENT_TABLE_NAME          = aws_dynamodb_table.incidents.name
      LIFECYCLE_TABLE_NAME         = aws_dynamodb_table.asg_lifecycle_events.name
      SLACK_WEBHOOK_URL            = var.slack_webhook_url
      TARGET_GROUP_ARN             = var.target_group_arn
      APP_HEALTHCHECK_URL          = var.app_healthcheck_url
      ROUTER_FUNCTION_NAME         = "${var.env_prefix}-incident-event-router"
      ASG_NAME                     = var.asg_name
      MAX_REMEDIATION_ATTEMPTS     = tostring(var.max_remediation_attempts)
      VERIFICATION_TIMEOUT_SECONDS = tostring(var.verification_timeout_seconds)
      MIN_HEALTHY_TARGETS          = tostring(var.min_healthy_targets)
    }
  }
  depends_on = [aws_cloudwatch_log_group.worker_logs]
  tags       = var.tags
}
resource "aws_lambda_function_event_invoke_config" "worker_async_failures" {
  function_name                = aws_lambda_function.remediation_worker.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
  destination_config {
    on_failure {
      destination = aws_sqs_queue.worker_failure_dlq.arn
    }
  }
}

resource "aws_lambda_function" "incident_dispatcher" {
  filename         = data.archive_file.dispatcher_zip.output_path
  function_name    = "${var.env_prefix}-incident-dispatcher"
  role             = aws_iam_role.dispatcher_role.arn
  handler          = "dispatcher.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  source_code_hash = data.archive_file.dispatcher_zip.output_base64sha256
  environment {
    variables = {
      INCIDENT_TABLE_NAME = aws_dynamodb_table.incidents.name
      WORKER_FUNCTION_ARN = aws_lambda_function.remediation_worker.arn
      ENV_PREFIX          = var.env_prefix
      INCIDENT_TTL_DAYS   = tostring(var.incident_ttl_days)
    }
  }
  depends_on = [aws_cloudwatch_log_group.dispatcher_logs]
  tags       = var.tags
}
resource "aws_lambda_permission" "allow_sns_dispatcher" {
  statement_id  = "AllowCloudWatchSNSInvokeDispatcher"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_dispatcher.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cloudwatch_alarms_topic.arn
}
resource "aws_sns_topic_subscription" "dispatcher_subscription" {
  topic_arn  = aws_sns_topic.cloudwatch_alarms_topic.arn
  protocol   = "lambda"
  endpoint   = aws_lambda_function.incident_dispatcher.arn
  depends_on = [aws_lambda_permission.allow_sns_dispatcher]
}

# Target-health events carry the target ID; lifecycle events let the worker distinguish
# native Auto Scaling replacement from a termination requested by the worker.
resource "aws_iam_role" "event_router_role" {
  name = "${var.env_prefix}-incident-event-router-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}
resource "aws_iam_role_policy_attachment" "event_router_logs" {
  role       = aws_iam_role.event_router_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "event_router_policy" {
  name = "${var.env_prefix}-incident-event-router-policy"
  role = aws_iam_role.event_router_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.incidents.arn },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"], Resource = aws_dynamodb_table.asg_lifecycle_events.arn },
      { Effect = "Allow", Action = ["dynamodb:GetItem"], Resource = aws_dynamodb_table.incidents.arn },
      { Effect = "Allow", Action = ["autoscaling:DescribeAutoScalingGroups"], Resource = "*" },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricAlarm"], Resource = "*" },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*" },
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = aws_lambda_function.remediation_worker.arn }
    ]
  })
}
resource "aws_cloudwatch_log_group" "event_router_logs" {
  name              = "/aws/lambda/${var.env_prefix}-incident-event-router"
  retention_in_days = var.log_retention_in_days
  tags              = var.tags
}
resource "aws_lambda_function" "incident_event_router" {
  filename         = data.archive_file.event_router_zip.output_path
  function_name    = "${var.env_prefix}-incident-event-router"
  role             = aws_iam_role.event_router_role.arn
  handler          = "event_router.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  source_code_hash = data.archive_file.event_router_zip.output_base64sha256
  environment {
    variables = {
      INCIDENT_TABLE_NAME  = aws_dynamodb_table.incidents.name
      LIFECYCLE_TABLE_NAME = aws_dynamodb_table.asg_lifecycle_events.name
      WORKER_FUNCTION_ARN  = aws_lambda_function.remediation_worker.arn
      ASG_NAME             = var.asg_name
      ENV_PREFIX           = var.env_prefix
      INCIDENT_TTL_DAYS    = tostring(var.incident_ttl_days)
      SLACK_WEBHOOK_URL    = var.slack_webhook_url
    }
  }
  depends_on = [aws_cloudwatch_log_group.event_router_logs]
  tags       = var.tags
}
resource "aws_cloudwatch_event_rule" "elb_target_health_unhealthy" {
  name        = "${var.env_prefix}-elb-target-health-unhealthy"
  description = "Capture target-specific ELB unhealthy transitions for incident correlation."
  event_pattern = jsonencode({
    source        = ["aws.elasticloadbalancing"]
    "detail-type" = ["ELB Target Health State Change"]
    detail = {
      "target-group-arn" = [var.target_group_arn]
      "target-health"    = { state = ["unhealthy"] }
    }
  })
  tags = var.tags
}
resource "aws_cloudwatch_event_target" "elb_target_health_router" {
  rule      = aws_cloudwatch_event_rule.elb_target_health_unhealthy.name
  target_id = "incident-event-router-target-health"
  arn       = aws_lambda_function.incident_event_router.arn
}
resource "aws_lambda_permission" "allow_eventbridge_target_health" {
  statement_id  = "AllowEventBridgeTargetHealthRouter"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_event_router.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.elb_target_health_unhealthy.arn
}

# All environment alarm notifications are released only by CloudWatch state-change events.
# SNS is retained for incident dispatch; it is not a Slack notification path.
resource "aws_cloudwatch_event_rule" "target_incident_alarm_state" {
  name        = "${var.env_prefix}-cloudwatch-alarm-state-notifications"
  description = "Route ALARM and OK transitions for all environment alarms after CloudWatch records the state change."
  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail = {
      state     = { value = ["ALARM", "OK"] }
      alarmName = [{ wildcard = "${var.env_prefix}-*" }]
    }
  })
  tags = var.tags
}

resource "aws_sqs_queue" "alarm_state_event_dlq" {
  name                      = "${var.env_prefix}-alarm-state-event-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_sqs_queue_policy" "alarm_state_event_dlq" {
  queue_url = aws_sqs_queue.alarm_state_event_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEventBridgeSendToAlarmStateDLQ"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.alarm_state_event_dlq.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.target_incident_alarm_state.arn } }
    }]
  })
}

resource "aws_cloudwatch_event_target" "target_incident_alarm_state_router" {
  rule      = aws_cloudwatch_event_rule.target_incident_alarm_state.name
  target_id = "incident-event-router-alarm-state"
  arn       = aws_lambda_function.incident_event_router.arn
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 12
  }
  dead_letter_config {
    arn = aws_sqs_queue.alarm_state_event_dlq.arn
  }
  depends_on = [aws_sqs_queue_policy.alarm_state_event_dlq]
}

resource "aws_cloudwatch_metric_alarm" "alarm_state_event_dlq_not_empty" {
  alarm_name          = "${var.env_prefix}-target-incident-alarm-dlq-not-empty"
  alarm_description   = "One or more CloudWatch alarm-state events could not be delivered to the incident router. Inspect the DLQ before redriving messages."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  dimensions = {
    QueueName = aws_sqs_queue.alarm_state_event_dlq.name
  }
  alarm_actions = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags          = var.tags
}

resource "aws_lambda_permission" "allow_eventbridge_alarm_state" {
  statement_id  = "AllowEventBridgeTargetIncidentAlarmState"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_event_router.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.target_incident_alarm_state.arn
}
resource "aws_cloudwatch_event_target" "asg_launch_lifecycle_router" {
  rule      = aws_cloudwatch_event_rule.asg_launch_success.name
  target_id = "incident-event-router-launch-lifecycle"
  arn       = aws_lambda_function.incident_event_router.arn
}
resource "aws_cloudwatch_event_target" "asg_terminate_lifecycle_router" {
  rule      = aws_cloudwatch_event_rule.asg_terminate_success.name
  target_id = "incident-event-router-terminate-lifecycle"
  arn       = aws_lambda_function.incident_event_router.arn
}
resource "aws_lambda_permission" "allow_eventbridge_lifecycle" {
  statement_id  = "AllowEventBridgeLifecycleRouter"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_event_router.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.asg_launch_success.arn
}
resource "aws_lambda_permission" "allow_eventbridge_lifecycle_terminate" {
  statement_id  = "AllowEventBridgeLifecycleTerminateRouter"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.incident_event_router.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.asg_terminate_success.arn
}

# Per-instance CPU alarms are created after successful ASG launches and deleted after termination.
resource "aws_iam_role" "alarm_manager_role" {
  name = "${var.env_prefix}-cpu-alarm-manager-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = var.tags
}
resource "aws_iam_role_policy_attachment" "alarm_manager_logs" {
  role       = aws_iam_role.alarm_manager_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "alarm_manager_policy" {
  name = "${var.env_prefix}-cpu-alarm-manager-policy"
  role = aws_iam_role.alarm_manager_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms"], Resource = "*" },
      { Effect = "Allow", Action = ["autoscaling:DescribeAutoScalingGroups"], Resource = "*" }
    ]
  })
}
resource "aws_cloudwatch_log_group" "alarm_manager_logs" {
  name              = "/aws/lambda/${var.env_prefix}-cpu-alarm-manager"
  retention_in_days = var.log_retention_in_days
  tags              = var.tags
}
resource "aws_lambda_function" "cpu_alarm_manager" {
  filename         = data.archive_file.alarm_manager_zip.output_path
  function_name    = "${var.env_prefix}-cpu-alarm-manager"
  role             = aws_iam_role.alarm_manager_role.arn
  handler          = "alarm_manager.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  source_code_hash = data.archive_file.alarm_manager_zip.output_base64sha256
  environment {
    variables = {
      ALARM_PREFIX            = var.env_prefix
      ASG_NAME                = var.asg_name
      SNS_TOPIC_ARN           = aws_sns_topic.cloudwatch_alarms_topic.arn
      CPU_ALARM_THRESHOLD     = tostring(var.cpu_alarm_threshold)
      CPU_ALARM_PERIOD        = tostring(var.cpu_alarm_period)
      CPU_EVALUATION_PERIODS  = tostring(var.cpu_evaluation_periods)
      CPU_DATAPOINTS_TO_ALARM = tostring(var.cpu_datapoints_to_alarm)
    }
  }
  depends_on = [aws_cloudwatch_log_group.alarm_manager_logs]
  tags       = var.tags
}
resource "aws_cloudwatch_event_rule" "asg_launch_success" {
  name = "${var.env_prefix}-asg-instance-launch-success"
  event_pattern = jsonencode({
    source        = ["aws.autoscaling"]
    "detail-type" = ["EC2 Instance Launch Successful"]
    detail        = { AutoScalingGroupName = [var.asg_name] }
  })
  tags = var.tags
}
resource "aws_cloudwatch_event_rule" "asg_terminate_success" {
  name = "${var.env_prefix}-asg-instance-terminate-success"
  event_pattern = jsonencode({
    source        = ["aws.autoscaling"]
    "detail-type" = ["EC2 Instance Terminate Successful"]
    detail        = { AutoScalingGroupName = [var.asg_name] }
  })
  tags = var.tags
}
resource "aws_cloudwatch_event_target" "launch_alarm_manager" {
  rule      = aws_cloudwatch_event_rule.asg_launch_success.name
  target_id = "cpu-alarm-manager-launch"
  arn       = aws_lambda_function.cpu_alarm_manager.arn
}
resource "aws_cloudwatch_event_target" "terminate_alarm_manager" {
  rule      = aws_cloudwatch_event_rule.asg_terminate_success.name
  target_id = "cpu-alarm-manager-terminate"
  arn       = aws_lambda_function.cpu_alarm_manager.arn
}
resource "aws_lambda_permission" "allow_eventbridge_launch" {
  statement_id  = "AllowEventBridgeASGLaunch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cpu_alarm_manager.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.asg_launch_success.arn
}
resource "aws_lambda_permission" "allow_eventbridge_terminate" {
  statement_id  = "AllowEventBridgeASGTerminate"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cpu_alarm_manager.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.asg_terminate_success.arn
}

data "archive_file" "dispatcher_zip" {
  type        = "zip"
  output_path = "${path.module}/dispatcher.zip"
  source {
    content  = file("${path.module}/dispatcher.py")
    filename = "dispatcher.py"
  }
}
data "archive_file" "worker_zip" {
  type        = "zip"
  output_path = "${path.module}/remediation_worker.zip"
  source {
    content  = file("${path.module}/remediation_worker.py")
    filename = "remediation_worker.py"
  }
}
data "archive_file" "alarm_manager_zip" {
  type        = "zip"
  output_path = "${path.module}/alarm_manager.zip"
  source {
    content  = file("${path.module}/alarm_manager.py")
    filename = "alarm_manager.py"
  }
}
data "archive_file" "event_router_zip" {
  type        = "zip"
  output_path = "${path.module}/event_router.zip"
  source {
    content  = file("${path.module}/event_router.py")
    filename = "event_router.py"
  }
}

resource "aws_cloudwatch_event_rule" "alarm_reconciliation" {
  name                = "${var.env_prefix}-cpu-alarm-reconciliation"
  description         = "Periodically reconcile per-instance CPU alarms for current ASG members."
  schedule_expression = "rate(15 minutes)"
  tags                = var.tags
}
resource "aws_cloudwatch_event_target" "alarm_reconciliation" {
  rule      = aws_cloudwatch_event_rule.alarm_reconciliation.name
  target_id = "cpu-alarm-manager-reconcile"
  arn       = aws_lambda_function.cpu_alarm_manager.arn
}
resource "aws_lambda_permission" "allow_eventbridge_reconciliation" {
  statement_id  = "AllowEventBridgeAlarmReconciliation"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cpu_alarm_manager.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_reconciliation.arn
}

# Alarm on the incident pipeline itself so failures do not remain silent.
resource "aws_cloudwatch_metric_alarm" "dispatcher_errors" {
  alarm_name          = "${var.env_prefix}-Incident-Dispatcher-Errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.incident_dispatcher.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags                = var.tags
}
resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${var.env_prefix}-Remediation-Worker-Errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.remediation_worker.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags                = var.tags
}
resource "aws_cloudwatch_metric_alarm" "worker_dlq_visible" {
  alarm_name          = "${var.env_prefix}-Remediation-DLQ-Not-Empty"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.worker_failure_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags                = var.tags
}
