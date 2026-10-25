# Notification pipeline refactor report

**Scope:** Review of the uploaded `aws-automated-incident-remediation-main` repository after reports that CloudWatch ALB alarms and Auto Scaling replacements were not producing Slack notifications.

## Root causes found

1. **The dispatcher deliberately discarded ALB alarm notifications.** In `modules/monitoring/dispatcher.py`, group-scoped alarms classified as `alb_health` were logged and skipped. If the target-specific EventBridge path did not deliver or the router failed before its Slack call, the CloudWatch alarm could enter `ALARM` without any Slack message.
2. **The EventBridge lifecycle handler did not notify Slack.** `event_router.py::_write_lifecycle` only persisted launch/termination events in DynamoDB. It had no Slack notification path, so a successful replacement launch was silent.
3. **Detection notifications were sent after asynchronous worker invocation.** Both the dispatcher and target-health router attempted Slack notification after `lambda.invoke`. If invocation raised an error, control exited before the initial detection message was sent.
4. **The event-router role lacked DynamoDB permissions used by retry code.** The handler reads incident records and reads/updates lifecycle records, but the role did not grant all required `GetItem`/`UpdateItem` actions. That could make duplicate/retry paths fail with `AccessDenied`.

## Follow-up findings for “Slack received, CloudWatch alarm absent”

- The Terraform source did contain a persistent group-level alarm named `${env_prefix}-ALB-Unhealthy-Targets`; it did not define an alarm named `prod-ALB-UnHealthyHostCount`. With `env_prefix=prod`, the expected native alarm name is `prod-ALB-Unhealthy-Targets`.
- The target-health EventBridge route sent Slack directly and wrote an incident record, but it did not create a CloudWatch alarm for the specific instance. Therefore, Slack could arrive even when the static Terraform alarm had not been applied in the account/region being inspected.
- Source inspection alone cannot determine why the static alarm was absent from the live account; likely checks include incomplete apply, workspace/account/region mismatch, or deployed code differing from the repository. The workflow previously did not assert that the alarm existed after apply.

## Changes made

- Added a deduplicated, `ALARM_NOTIFICATION_ONLY` fallback record and Slack notification for CloudWatch ALB alarms. It does **not** invoke the worker; the target-specific EventBridge incident remains the only ALB route that can initiate remediation.
- Moved the dispatcher’s regular incident notification attempt before worker invocation.
- Added notification retry behavior for duplicate SNS deliveries when an incident exists but its initial Slack alert was not recorded as delivered.
- Moved the target-specific ALB detection alert attempt before worker invocation. If the worker dispatch fails, the incident alert has already been attempted and the incident is marked `DISPATCH_FAILED` for retry.
- Added Slack notifications for successful Auto Scaling launch/termination lifecycle events. A launch notification includes the new instance ID and explicitly states that launch is not proof of recovery. The worker’s final notification remains responsible for the verified recovery verdict.
- Persisted lifecycle notification state (`PENDING`, `DELIVERED`, or `FAILED`) and recorded send errors. Failed lifecycle notifications raise after recording failure so EventBridge can retry delivery.
- Added the event-router role’s missing DynamoDB `GetItem` and `UpdateItem` permissions for the incident/lifecycle paths.
- Updated static tests and operations documentation. Changed Terraform CI formatting to recurse through modules and removed a premature “teardown succeeded” Slack notification that ran before the destroy step.

## Validation performed

- Python source compilation: passed.
- `python -m unittest discover -s tests -v`: **29 tests passed** after the follow-up CloudWatch alarm changes.
- `bash -n entry-script.sh`: passed.
- Both GitHub Actions workflow YAML files parsed successfully.

## Not performed

Terraform CLI was not available in the audit environment, so `terraform fmt`, `terraform init`, `terraform validate`, and `terraform plan` were not run. No AWS deployment or live SNS/EventBridge/Slack integration test was run. These source-level checks do not prove live delivery.

## Deployment and post-deployment checks

1. Review the Terraform plan in the intended HCP Terraform workspace/account/region before applying.
2. After apply, inspect:
   - `/aws/lambda/<env>-incident-dispatcher`
   - `/aws/lambda/<env>-incident-event-router`
   - `/aws/lambda/<env>-remediation-worker`
3. Confirm `SLACK_WEBHOOK_URL` is configured for all three Lambda functions and is a valid incoming webhook.
4. Trigger a controlled unhealthy-target test in a non-production environment. Confirm a CloudWatch alarm fallback notification, the target-specific incident record, the lifecycle launch notification, and the worker’s final verified result.
5. Check DynamoDB fields `incident_alert_status`, `notification_status`, `notification_error`, `replacement_instance_id`, and `recovery_status` when investigating any missing message.

The alarm-level fallback and target-specific EventBridge route can both send detection messages for the same unhealthy-target episode. They do not both invoke remediation; the fallback is explicitly notification-only. The lifecycle launch message reports a launch event, not verified recovery.


## Follow-up: CloudWatch alarm review for target-specific incidents

The repository already declared the native Terraform resource `${env_prefix}-ALB-Unhealthy-Targets`; its absence in the deployed console means that deployed Terraform state/configuration still needs to be reconciled with the repository. The target-health EventBridge route, however, can emit Slack independently of that static metric alarm. To make each target-specific incident directly reviewable in CloudWatch after this code is deployed, the event router now creates a retained custom metric alarm named `${env_prefix}-<instance-id>-Unhealthy-Target-<incident-suffix>` and publishes a datapoint in `SelfHealing/TargetHealth`. The alarm dimensions include `InstanceId` and `IncidentId`; its state history is distinct for each target event. It has alarm actions disabled to avoid sending a second Slack notification because the event router already sends the detection alert. The router role has the required `cloudwatch:PutMetricAlarm` and `cloudwatch:PutMetricData` permissions. If alarm creation fails, the incident record marks the failure and the Lambda invocation errors after the Slack/worker attempt so EventBridge can retry; duplicate-event handling retries alarm creation without redispatching the worker or resending an already-delivered Slack message.

The custom alarms are created at incident time, so they will not appear before a target-specific unhealthy event reaches the event router. The static native ALB alarm should be created by applying the Terraform configuration. Per-incident alarms and custom metrics are retained for review and can accumulate; remove them after the retention/review period if cost or alarm-count management requires it.


The deployment workflow now exports the exact Terraform-managed unhealthy-target alarm name and verifies after apply that the alarm exists in the runner's AWS account/`us-east-1` region and monitors `AWS/ApplicationELB/UnHealthyHostCount`. This turns a missing alarm into a visible deployment verification failure instead of a silent gap. The runner's OIDC role must have `cloudwatch:DescribeAlarms` permission.
