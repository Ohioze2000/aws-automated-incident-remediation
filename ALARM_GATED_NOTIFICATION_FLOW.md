# CloudWatch-gated notification flow

## Incident alarms (CPU, memory, disk, ALB, application errors)

1. CloudWatch evaluates the configured metric alarm and records a state transition (`ALARM` or `OK`).
2. CloudWatch emits `CloudWatch Alarm State Change` to EventBridge. The environment-prefixed alarm rule routes the event to the incident event router.
3. The router correlates and deduplicates the state transition in DynamoDB, then sends the corresponding Slack message. An `OK` notification is not sent until the prior `ALARM` notification has been delivered.
4. In parallel, SNS invokes the dispatcher for alarm notifications that have SNS actions. The dispatcher persists/deduplicates incidents and invokes the remediation worker; it does not send Slack.
5. A remediation-success Slack message is separate and is sent only after CloudWatch `OK` plus all configured recovery checks pass.

## Target-specific unhealthy events

1. EventBridge receives the ELB target-health event and the router persists the incident and original instance ID.
2. The router creates a per-incident CloudWatch alarm, persists the alarm-to-incident correlation, and publishes the custom metric.
3. Only the subsequent CloudWatch alarm-state event releases the incident-detection Slack notification. The per-incident alarm has alarm actions disabled to prevent a duplicate SNS path.

## Auto Scaling lifecycle events

1. EventBridge receives the ASG lifecycle event. The router persists it in the lifecycle table and a `LIFECYCLE-*` incident record.
2. The router creates a deterministic one-shot CloudWatch alarm and publishes a lifecycle metric. It does not send Slack at this stage.
3. After CloudWatch enters `ALARM`, the alarm-state event route sends the launch/termination operational message to Slack. A successful launch does not mean the application has recovered.

## Ordering guarantee and limits

Slack incident/recovery/lifecycle messages are triggered from CloudWatch Alarm State Change events, not directly from the original ELB/ASG event or SNS dispatcher. This means CloudWatch must first evaluate and record the relevant alarm transition. EventBridge delivery is at-least-once; DynamoDB idempotency suppresses duplicate state events, but Slack webhooks cannot provide strict exactly-once delivery if a process crashes after Slack accepts a message and before DynamoDB records success. The repository must still pass Terraform validation and live AWS integration tests after deployment.
