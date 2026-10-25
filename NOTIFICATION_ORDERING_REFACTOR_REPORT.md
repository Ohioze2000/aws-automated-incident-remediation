# Notification Ordering and Incident Correlation Refactor

## Objective

Keep useful Auto Scaling lifecycle notifications immediate while ensuring a target-specific incident-detection Slack message is emitted only after the corresponding per-incident CloudWatch alarm has entered `ALARM`.

## Changes

1. **Alarm-gated incident alert:** target-health EventBridge processing no longer sends the target-specific incident alert directly. A separate `CloudWatch Alarm State Change` route sends it only for a matching per-incident alarm in `ALARM`.
2. **Strong incident correlation:** the per-incident alarm name includes the complete incident ID (`INC-` plus 24 hexadecimal characters). The state-change handler parses that ID and reads the durable incident record.
3. **Persist-before-publish ordering:** the router creates the alarm, persists `cloudwatch_alarm_name` and `cloudwatch_alarm_status=ALARM_CREATED`, and only then publishes the custom metric. The state-change event therefore has a durable correlation record to look up.
4. **Lifecycle notifications retained:** successful and unsuccessful Auto Scaling launch/termination lifecycle events continue to be persisted and sent to Slack independently. Message formatting uses actual newlines. These are clearly informational and do not claim recovery.
5. **Retry and dead-letter handling:** the alarm-state EventBridge target has a bounded retry policy and a dedicated encrypted SQS DLQ. A CloudWatch alarm notifies the existing SNS topic if messages accumulate in that DLQ.
6. **Notification delivery state:** incident records track `incident_alert_status`, claim timestamps, delivery timestamps, and errors. Conditional DynamoDB updates suppress concurrent duplicate sends; a stale claim can be recovered after a lease timeout.
7. **No second remediation worker:** the target-health route remains the only ALB path that initiates the worker. The native group-level CloudWatch alarm notification remains notification-only.
8. **Recovery remains separate:** lifecycle launch/termination and alarm detection do not count as recovery. The worker's final outcome must continue to depend on target health and capacity verification.
9. **Documentation and contract tests:** README/runbook and static contract tests now describe and enforce the alarm-gated notification contract.

## Intended ordering

- Auto Scaling lifecycle updates may arrive immediately before or after the incident alert. They are deliberately retained and are not incident-detection claims.
- A target-specific incident alert must follow the matching per-incident CloudWatch alarm's transition to `ALARM`.
- Remediation can proceed without waiting for Slack delivery; notification ordering is not allowed to delay safety actions.
- Recovery confirmation is sent only after the worker's verification checks pass.

## Validation performed

- `python -m unittest discover -s tests -v`: **30 tests passed**.
- Python compilation (`python -m compileall -q modules/monitoring tests`): passed.
- Bootstrap shell syntax (`bash -n entry-script.sh`): passed.
- Both GitHub Actions workflow YAML files parsed successfully.

## Validation not performed

- Terraform CLI was not available in this environment. `terraform fmt -check -recursive`, `terraform init`, `terraform validate`, and `terraform plan` were not run.
- No live AWS deployment, CloudWatch alarm-state event, SNS/EventBridge delivery, Slack webhook, or recovery integration test was performed.

These results establish source-level contract checks only; they do not prove live AWS behavior. Review the Terraform plan in the intended HCP Terraform workspace before applying.

## Deployment and test plan

1. Review the diff and confirm the HCP Terraform workspace, AWS account, region (`us-east-1`), environment prefix, and target-group ARN.
2. Run `terraform fmt -check -recursive`, `terraform init`, `terraform validate`, and a plan in the repository's supported Terraform environment.
3. Review the plan for the new `${env_prefix}-target-incident-alarm-state` EventBridge rule, `${env_prefix}-alarm-state-event-dlq`, queue policy, and DLQ depth alarm. Do not apply an unexpected replacement or destruction.
4. Apply through the normal approved HCP Terraform workflow.
5. In a controlled non-production test, cause a target-health unhealthy event and verify: incident record persisted; per-incident alarm created; alarm-to-incident mapping persisted before metric publication; alarm transitions to `ALARM`; only then the target-specific incident alert appears in Slack.
6. Verify Auto Scaling launch/termination Slack updates still arrive independently and remain explicitly non-recovery messages.
7. Inspect `/aws/lambda/<env>-incident-event-router`, alarm history, the incident DynamoDB item, EventBridge rule metrics, and the DLQ. Confirm the worker is invoked at most once for the incident and recovery is reported only after target health and capacity checks pass.

## Operational caveat

AWS EventBridge delivery and Lambda processing are at-least-once. DynamoDB idempotency reduces duplicate notifications, but a Slack incoming webhook does not support a transactional exactly-once guarantee. A process crash after Slack accepts a message but before DynamoDB records `DELIVERED` can still cause a duplicate on retry. The implementation records this state and retries failures rather than claiming exactly-once delivery.
