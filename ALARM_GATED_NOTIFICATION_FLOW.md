# CloudWatch-gated Slack notification sequence

For correlated per-incident ALB target-health incidents, incident lifecycle Slack messages follow this sequence:

1. **Incident detected:** the event router sends the detection Slack message only after the incident-specific CloudWatch alarm emits an `ALARM` state-change event.
2. **CloudWatch alarm recovered:** the event router sends a distinct `ALARM → OK` message only after CloudWatch emits the correlated alarm's `OK` state-change event. The text explicitly says application verification is pending.
3. **Remediation successful — recovery verified:** the worker records its verification evidence and sends an internal completion event to the router. The router sends the success Slack message only when the DynamoDB incident record confirms all of the following: CloudWatch alarm state is `OK`, the recovery notification was delivered, incident status is `RECOVERY_VERIFIED`, target health passed, application HTTP probe passed, capacity passed, and a replacement instance ID is present.

The worker cannot send the verified-success Slack message directly. If verification completes before CloudWatch reaches `OK`, the success notification is deferred. If CloudWatch reaches `OK` first, the later worker completion event releases the success notification. DynamoDB notification status fields suppress duplicate success messages and preserve retryability.

This sequence is implemented for the correlated per-incident target-health alarms named with `-Unhealthy-Target-INC-...`. The group-level ALB alarm remains notification-only. This is source-level behavior; validate Terraform and the full sequence in AWS before treating it as production-validated. EventBridge and Lambda are at-least-once systems, and Slack webhooks cannot provide a strict exactly-once guarantee if Slack accepts a message but the delivery-status write fails.
