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
