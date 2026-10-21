# ==============================================================================
# 1. CLOUDWATCH LOG GROUPS
# ==============================================================================

resource "aws_cloudwatch_log_group" "app_log_groups" {
  for_each = var.log_groups

  name              = "/aws/${var.env_prefix}/${each.key}"
  retention_in_days = coalesce(each.value.retention_in_days, var.default_log_retention_days)
  kms_key_id        = try(each.value.kms_key_id, var.kms_key_id, null)

  tags = merge(
    var.tags,
    {
      Name        = "${var.env_prefix}-${each.key}-logs"
      Environment = var.env_prefix
    }
  )
}

# ==============================================================================
# 2. METRIC FILTERS (Extract metrics from logs)
# ==============================================================================

resource "aws_cloudwatch_log_metric_filter" "filters" {
  for_each = var.metric_filters

  name           = "${var.env_prefix}-${each.key}"
  pattern        = each.value.pattern
  log_group_name = aws_cloudwatch_log_group.app_log_groups[each.value.log_group_key].name

  metric_transformation {
    name          = each.value.metric_name
    namespace     = each.value.metric_namespace
    value         = each.value.metric_value
    default_value = each.value.default_value
  }
}

# ==============================================================================
# 3. APPLICATION ALARMS
# ==============================================================================

resource "aws_cloudwatch_metric_alarm" "app_alarms" {
  for_each = var.app_alarms

  alarm_name          = "${var.env_prefix}-${each.key}"
  comparison_operator = each.value.comparison_operator
  evaluation_periods  = each.value.evaluation_periods
  metric_name         = each.value.metric_name
  namespace           = each.value.metric_namespace
  period              = each.value.period
  statistic           = each.value.statistic
  threshold           = each.value.threshold
  alarm_description   = coalesce(each.value.description, "Alarm triggered for ${each.key}")
  treat_missing_data  = each.value.treat_missing_data

  alarm_actions = var.alarm_sns_topic_arns
  ok_actions    = var.alarm_sns_topic_arns

  dimensions = each.value.dimensions

  tags = merge(
    var.tags,
    {
      Name        = "${var.env_prefix}-${each.key}-alarm"
      Environment = var.env_prefix
    }
  )

  depends_on = [aws_cloudwatch_log_metric_filter.filters]
}