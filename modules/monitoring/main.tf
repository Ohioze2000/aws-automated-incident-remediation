# ==============================================================================
# 1. CORE NOTIFICATION INFRASTRUCTURE
# ==============================================================================

resource "aws_sns_topic" "cloudwatch_alarms_topic" {
  name              = "${var.env_prefix}-cloudwatch-alarms"
  display_name      = "${var.env_prefix} CloudWatch Alarms"
  kms_master_key_id = var.sns_kms_key_id

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-cloudwatch-alarms"
    }
  )
}

# SNS Topic Policy allowing CloudWatch to publish alarms securely
resource "aws_sns_topic_policy" "default" {
  arn = aws_sns_topic.cloudwatch_alarms_topic.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowCloudWatchToPublish"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cloudwatch_alarms_topic.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:cloudwatch:*:*:alarm:${var.env_prefix}-*"
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "email_subscription" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.cloudwatch_alarms_topic.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ==============================================================================
# 2. SSM PARAMETER STORE (CLOUDWATCH AGENT CONFIGURATION)
# ==============================================================================

resource "aws_ssm_parameter" "cw_agent_config" {
  name        = "/asg-webserver/cloudwatch-agent-config"
  description = "CloudWatch Agent configuration JSON for EC2 instances in Auto Scaling Group"
  type        = "String"

  value = jsonencode({
    agent = {
      metrics_collection_interval = 60,
      run_as_user                 = "root"
    }
    metrics = {
      namespace = "CWAgent"
      append_dimensions = {
        InstanceId           = "$${aws:InstanceId}"
        AutoScalingGroupName = "$${aws:AutoScalingGroupName}"
      }
      aggregation_dimensions = [
        ["InstanceId"],
        ["AutoScalingGroupName", "path", "fstype"]
      ]
      metrics_collected = {
        cpu = {
          metrics_collection_interval = 60
          measurement = [
            "cpu_usage_idle",
            "cpu_usage_iowait",
            "cpu_usage_user",
            "cpu_usage_system"
          ]
          totalcpu = true
        }
        disk = {
          metrics_collection_interval = 60
          resources                   = ["/"]
          measurement                 = ["disk_used_percent", "inodes_free"]
          ignore_file_system_types    = ["sysfs", "devtmpfs", "tmpfs"]
          drop_device                 = true
        }
        mem = {
          metrics_collection_interval = 60
          measurement                 = ["mem_used_percent"]
        }
        swap = {
          metrics_collection_interval = 60
          measurement                 = ["swap_used_percent"]
        }
      }
    }
    logs = {
      logs_collected = {
        files = {
          collect_list = [
            {
              file_path         = "/var/log/cloud-init-output.log"
              log_group_name    = "/ec2/cloud-init-output"
              log_stream_name   = "{instance_id}"
              retention_in_days = var.log_retention_in_days
            },
            {
              file_path         = "/var/log/user-data.log"
              log_group_name    = "/ec2/user-data"
              log_stream_name   = "{instance_id}"
              retention_in_days = var.log_retention_in_days
            },
            {
              file_path         = "/var/log/nginx/access.log"
              log_group_name    = "/ec2/nginx/access"
              log_stream_name   = "{instance_id}"
              retention_in_days = var.log_retention_in_days
            },
            {
              file_path         = "/var/log/nginx/error.log"
              log_group_name    = "/ec2/nginx/error"
              log_stream_name   = "{instance_id}"
              retention_in_days = var.log_retention_in_days
            },
            {
              file_path         = "/var/log/app/*.log"
              log_group_name    = "/aws/${var.env_prefix}/web_app"
              log_stream_name   = "{instance_id}"
              retention_in_days = var.log_retention_in_days
            }
          ]
        }
      }
    }
  })
}
# ==============================================================================
# 3. METRIC ALARMS
# ==============================================================================

# CPU alarms are managed per instance by the EventBridge-driven alarm manager.

resource "aws_cloudwatch_metric_alarm" "high_memory_alarm" {
  alarm_name          = "${var.env_prefix}-ASG-High-Memory-Utilization"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "mem_used_percent"
  namespace           = "CWAgent"
  period              = 120
  statistic           = "Average"
  threshold           = var.memory_alarm_threshold
  alarm_description   = "Memory usage across the Auto Scaling Group exceeds the configured threshold."
  treat_missing_data  = "missing"
  dimensions          = { AutoScalingGroupName = var.asg_name }
  alarm_actions       = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  ok_actions          = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags                = merge(var.tags, { Name = "${var.env_prefix}-ASG-High-Memory-Alarm" })
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_alb_alarm" {
  alarm_name          = "${var.env_prefix}-ALB-Unhealthy-Targets"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "UnHealthyHostCount"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Maximum"
  threshold           = 1
  alarm_description   = "At least one unhealthy target was observed in the application target group."
  treat_missing_data  = "notBreaching"
  dimensions = {
    TargetGroup  = var.target_group_dimension
    LoadBalancer = var.load_balancer_dimension
  }
  alarm_actions = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  ok_actions    = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  tags          = merge(var.tags, { Name = "${var.env_prefix}-ALB-Unhealthy-Targets" })
}

# --- HIGH DISK UTILIZATION ALARM ---
resource "aws_cloudwatch_metric_alarm" "high_disk_alarm" {
  count               = var.enable_disk_alarm ? 1 : 0
  alarm_name          = "${var.env_prefix}-ASG-High-Disk-Utilization"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = var.disk_evaluation_periods
  metric_name         = "disk_used_percent"
  namespace           = "CWAgent"
  period              = var.disk_alarm_period
  statistic           = "Average"
  threshold           = var.disk_alarm_threshold
  alarm_description   = "Alarm when root disk utilization exceeds ${var.disk_alarm_threshold}%"
  actions_enabled     = true
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = var.asg_name
    path                 = "/"
    fstype               = var.disk_fstype # Default variable e.g. "ext4" or "xfs"
  }

  alarm_actions = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  ok_actions    = [aws_sns_topic.cloudwatch_alarms_topic.arn]

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-ASG-High-Disk-Alarm"
    }
  )
}

# ==============================================================================
# 4. LOG ERROR MONITORING (FILTER + ALARM)
# ==============================================================================

resource "aws_cloudwatch_log_group" "application_logs" {
  name              = var.app_log_group_name
  retention_in_days = var.log_retention_in_days
  tags              = merge(var.tags, { Name = "${var.env_prefix}-application-logs" })
}

resource "aws_cloudwatch_log_metric_filter" "app_error_filter" {
  count          = var.enable_log_error_alarm ? 1 : 0
  name           = "${var.env_prefix}-app-error-filter"
  pattern        = var.log_error_pattern
  log_group_name = var.app_log_group_name
  depends_on     = [aws_cloudwatch_log_group.application_logs]

  metric_transformation {
    name          = "${var.env_prefix}-AppErrorCount"
    namespace     = "CustomAppMetrics"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_error_alarm" {
  count               = var.enable_log_error_alarm ? 1 : 0
  alarm_name          = "${var.env_prefix}-App-Log-Errors-Spike"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.app_error_filter[0].metric_transformation[0].name
  namespace           = aws_cloudwatch_log_metric_filter.app_error_filter[0].metric_transformation[0].namespace
  period              = var.log_error_alarm_period
  statistic           = "Sum"
  threshold           = var.log_error_threshold
  alarm_description   = "Alarm when application error occurrences in log group '${var.app_log_group_name}' exceed ${var.log_error_threshold}"
  actions_enabled     = true
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.cloudwatch_alarms_topic.arn]
  ok_actions    = [aws_sns_topic.cloudwatch_alarms_topic.arn]

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-App-Log-Errors-Alarm"
    }
  )
}

# ==============================================================================
