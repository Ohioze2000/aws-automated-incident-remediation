# Incident remediation operations runbook

## 1. Triage a detected incident

1. Open the incident record in the `${ENV_PREFIX}-incidents` DynamoDB table using the `incident_id` from the Slack notification.
2. Review `signal_type`, `alarm_name`, `instance_id`, `dimensions`, `reason`, `status`, `attempt_count`, `remediation_result`, and `verification_result`.
3. Confirm the corresponding CloudWatch alarm history and the dispatcher/worker CloudWatch log events.
4. Do not manually repeat an action solely because a Slack notification is missing. Check the incident status and AWS resource state first.

## 2. Interpret terminal states

- `ACTION_CONFIRMED`: the worker's action-specific verification passed. Review the evidence in `verification_result`.
- `OBSERVE_ONLY` / `NO_ACTION`: no disruptive action was taken; investigate the signal and follow the service's operational procedure.
- `BLOCKED_SAFETY_GUARD`: an explicit safety precondition prevented an action. Do not bypass the guard without reviewing service capacity and incident context.
- `VERIFICATION_FAILED`: the requested action did not meet its recovery criteria within the timeout. Treat the service as not recovered until independently checked.
- `FAILED` / `DISPATCH_FAILED`: inspect Lambda logs, IAM permissions, the incident item, and the worker failure DLQ before considering a retry.

## 3. Safe manual recovery

Before any manual termination, restart, cleanup, or replay:

- Check ALB healthy targets and application health.
- Check ASG desired/minimum/maximum capacity and lifecycle state.
- Check whether a deployment or maintenance operation is active.
- Confirm the target instance and incident ID to avoid acting on an unrelated resource.
- Preserve relevant logs and metrics before changing the system.

## 4. DLQ and dispatch failures

The remediation worker's asynchronous invocation failure destination is `${ENV_PREFIX}-remediation-worker-dlq`. Inspect the original failure and current incident/resource state before redriving a message. A DLQ message is evidence of a failed invocation, not permission to repeat a potentially disruptive action blindly.

## 5. Validate a deployment

After applying Terraform, verify:

- The alarm manager can describe, create/update, and delete alarms.
- A per-instance CPU alarm exists for each InService instance and has the expected `InstanceId` dimension.
- Alarm actions reference the intended SNS topic and the dispatcher subscription is confirmed.
- Dispatcher and worker roles have only the permissions required by their functions.
- The incident table TTL attribute matches the code's `ttl` field.
- The worker failure DLQ is empty or every message is understood and resolved.
- Test incidents in a non-production environment before enabling disruptive actions in production.


## 6. If detection or replacement notifications are missing

1. Check the CloudWatch alarm's `StateChangeTime`, `AlarmActions`, and whether the SNS topic delivered the message.
2. Inspect `/aws/lambda/<env>-incident-dispatcher`. For an ALB alarm, expect a log similar to `ALB alarm notification delivered`; the dispatcher sends a notification-only fallback and must not invoke the remediation worker for that group-level alarm.
3. Inspect `/aws/lambda/<env>-incident-event-router`. Confirm the EventBridge target-health rule matched the event, the target ID was an EC2 instance ID, and the incident was written before the detection alert was attempted.
4. For launch/termination notifications, inspect the lifecycle table item in `${ENV_PREFIX}-asg-lifecycle-events`. `notification_status=FAILED` and `notification_error` indicate a failed Slack request; the EventBridge target should retry the event.
5. For final recovery, inspect `/aws/lambda/<env>-remediation-worker` and the incident's `recovery_status`, `replacement_instance_id`, `verification_result`, and `notification_status`.
6. Confirm `SLACK_WEBHOOK_URL` is configured on dispatcher, event-router, and worker Lambda environments and that the webhook is valid. Do not paste the webhook into logs or issue reports.

A launch notification means that Auto Scaling launched an instance; it is not a recovery success claim. Recovery is verified only when the worker confirms the replacement target is healthy and the capacity safeguards pass.


## Reviewing an unhealthy EC2 target in CloudWatch

1. In the deployed region, open **CloudWatch → Alarms → All alarms** and search for `prod-ALB-Unhealthy-Targets` (or your environment prefix). This is the persistent target-group-level AWS/ApplicationELB alarm provisioned by Terraform.
2. For the individual target incident, search for `<env>-<instance-id>-Unhealthy-Target-<suffix>`. These per-incident alarms use namespace `SelfHealing/TargetHealth`, metric `UnhealthyTargetDetected`, and dimensions `Environment`, `TargetGroupArn`, `InstanceId`, and `IncidentId`. They are created when the target-health EventBridge event reaches the incident router and are retained for review.
3. Open the per-incident alarm and inspect **History** for its transition to `ALARM`; the matching event-router log entry and DynamoDB incident item contain the full event reason and incident context.
4. The custom per-incident alarm has actions disabled intentionally. The EventBridge router sends the detection Slack message, while the alarm exists as a CloudWatch review surface. The native group alarm continues to publish its alarm-state notifications to SNS.
5. If Slack arrives but no per-incident alarm exists, inspect `/aws/lambda/<env>-incident-event-router` for `CloudWatch per-instance alarm creation/publication failed`, then check the router role for `cloudwatch:PutMetricAlarm` and `cloudwatch:PutMetricData`. The incident record stores `cloudwatch_alarm_status` and, on failure, `cloudwatch_alarm_error`.
