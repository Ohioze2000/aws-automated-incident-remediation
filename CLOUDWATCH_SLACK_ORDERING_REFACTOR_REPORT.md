# CloudWatch-first Slack notification refactor report

## Problem found

The prior repository still had independent direct-to-Slack paths: the SNS dispatcher sent incident alerts without using the CloudWatch Alarm State Change route, and the Auto Scaling lifecycle handler sent launch/termination Slack messages directly from EventBridge. This allowed Slack messages to arrive without the corresponding alarm-state notification being the gating event.

## Changes made

- Made the CloudWatch Alarm State Change EventBridge rule cover all `${env_prefix}-*` alarms in `ALARM` and `OK`, not only per-target unhealthy alarms.
- Made EventBridge's alarm-state handler the notification owner for general alarm detection/recovery, existing target-specific incident alarms, and lifecycle-generated CloudWatch alarms.
- Removed direct Slack delivery from the SNS dispatcher. It now persists/deduplicates incident data and dispatches the remediation worker; it does not send Slack.
- Changed ASG launch/termination handling to persist lifecycle evidence, create a deterministic one-shot custom CloudWatch alarm, and publish a custom metric. The lifecycle handler does not send Slack. Its Slack notification is released only when CloudWatch emits the matching `ALARM` state-change event.
- Kept incident `ALARM` and recovery `OK` messages distinct. General alarm recovery notifications are retried if they fail; `OK` is not sent before the corresponding `ALARM` notification has been delivered.
- Kept the target-specific per-incident alarm route and verified-remediation-success gating.
- Updated README, runbook, flow documentation, and static contract tests to match the new contract.

## Validation performed

- Python AST/compile checks for Lambda sources.
- Static contract tests for notification routing and repository contracts.
- Shell syntax check and ZIP integrity check.

Terraform CLI validation/plan and live AWS/CloudWatch/EventBridge/SNS/Slack integration tests are not performed in this offline environment. Apply only after `terraform fmt -recursive`, `terraform init`, `terraform validate`, and a reviewed plan pass in your environment.
