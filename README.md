# AWS Self-Healing Infrastructure — Incident Pipeline Refactor

This repository provisions an AWS web tier with Terraform and includes a bounded, event-driven incident pipeline. It is a source refactor, **not a claim that AWS deployment or end-to-end recovery has been tested**. Review Terraform plans and validate in a disposable AWS environment before applying.

## Architecture

- **CPU:** EventBridge ASG lifecycle events call `alarm_manager.py`. It creates one `AWS/EC2` `CPUUtilization` alarm per instance with the `InstanceId` dimension. A 15-minute reconciliation run creates missing alarms and removes stale ones.
- **Memory and disk:** CloudWatch Agent publishes `CWAgent` metrics. Terraform alarms use the ASG roll-up dimensions; disk also matches `path=/` and the configured filesystem type.
- **ALB health:** Terraform provisions the persistent target-group-level `AWS/ApplicationELB/UnHealthyHostCount` alarm as `${env_prefix}-ALB-Unhealthy-Targets`. When a target-specific unhealthy event reaches EventBridge, the router persists the incident and creates a retained per-incident alarm named `${env_prefix}-<instance-id>-Unhealthy-Target-<incident-id>` in `SelfHealing/TargetHealth`. It persists the alarm-to-incident mapping before publishing the metric. A CloudWatch Alarm State Change EventBridge route sends the detection Slack message only after that correlated alarm enters `ALARM`; custom alarm actions stay disabled to avoid duplicate SNS notifications. The same route handles all `${env_prefix}-*` alarm ALARM/OK transitions; SNS is used for remediation dispatch only.
- **Application errors:** `modules/logging` owns the application log groups, metric filters, and alarms. Filter metric names/namespaces must match the configured alarms.
- **Incident pipeline:** CloudWatch alarm state transition → EventBridge alarm-state router → DynamoDB notification state → Slack. In parallel, alarm SNS actions → dispatcher Lambda → DynamoDB incident record → remediation worker → bounded action/observe-only decision → verification. The dispatcher never sends Slack directly; verified remediation success is sent only after CloudWatch `OK` and all required recovery checks pass. Dispatcher and worker failures/DLQ are monitored.
- **Safety:** Only a corroborated unhealthy ALB target can currently trigger the bounded replacement action. CPU, memory, disk, application-error, and unknown alarms are recorded and reported as observe-only; they do not automatically terminate or reboot an instance.

## Main refactor changes

1. `modules/logging` is the single Terraform owner for all `/aws/<environment>/...` log groups, filters, and app alarms. The monitoring module no longer creates a duplicate application log group or a competing generic error metric filter.
2. CloudWatch Agent group names match Terraform-created groups: `cloud-init-output`, `user-data`, `nginx/access`, `nginx/error`, and `web_app`.
3. The SSM parameter name is configurable and shared by the Terraform SSM resource, EC2 IAM policy, and `templatefile()` bootstrap. The IAM policy is scoped to the exact parameter ARN.
4. The EC2 launch template renders `entry-script.sh` with `templatefile()`. The script uses the configured archive URL and parameter name, enables an ERR trap, configures the agent before application deployment, validates the ZIP/Dockerfile, and checks the local HTTP endpoint.
5. Slack notifications use the `slack_webhook_url` sensitive Terraform input. Configure it as a **sensitive HCP Terraform workspace variable**; do not commit the real webhook URL. The URL is passed into Lambda environment variables and therefore remains sensitive state/configuration data accessible to sufficiently privileged administrators.
6. The CPU alarm manager's reconciliation also deletes stale per-instance CPU alarms when lifecycle-event delivery was missed. Per-incident unhealthy-target alarms are intentionally retained for post-incident CloudWatch history; remove them after review if you want to control alarm/metric accumulation.

## Configuration

- Copy `terraform.tfvars.example` to `terraform.tfvars` and replace every placeholder. The HCP Terraform backend in `backend.tf` targets organization `DigitalTech` and workspace `aws-automated-incident-remediation`; keep this aligned with the GitHub Actions workflow. After apply, CI verifies the native unhealthy-target alarm exists in the runner account and `us-east-1`; the OIDC role needs `cloudwatch:DescribeAlarms`.
- Set `slack_webhook_url` as a sensitive HCP Terraform workspace variable rather than placing a real webhook in a committed file.
- `app_archive_url` must be HTTPS and reachable from the EC2 instance. A private GitHub repository's ordinary raw/download URL will not work without authentication. Prefer a private S3 artifact plus narrowly scoped instance-role access for a production deployment, or use a valid short-lived signed URL only where its lifetime safely covers instance bootstrap.
- `cloudwatch_agent_parameter_name` defaults to `/asg-webserver/cloudwatch-agent-config`; if changed, the same input is passed to the SSM resource, EC2 IAM role, and bootstrap script.
- `metric_filters` must reference keys in `log_groups`/the built-in groups. The HTTP 5xx filter is attached to `nginx/access` because that is where the agent sends `/var/log/nginx/access.log`; database timeout/error filters are attached to `web_app`, which receives `/var/log/app/*.log`. If your existing `terraform.tfvars` still sets `http_5xx_errors.log_group_key = "web_app"`, change it to `"nginx/access"`. Verify patterns against actual log events and adapt them to the application's real log format.
- `disk_fstype` defaults to `ext4`; confirm the actual root filesystem and the metric's dimension values in CloudWatch before relying on the disk alarm.
- The root `my_ip` input is retained for compatibility with existing variable files but is not currently used by the modules in this source tree. Avoid opening SSH to `0.0.0.0/0`; prefer SSM Session Manager.

## Existing Terraform state: important log-group migration

The older code managed `/aws/<env>/web_app` at `module.monitoring.aws_cloudwatch_log_group.application_logs`. The refactored code manages it at `module.logging.aws_cloudwatch_log_group.app_log_groups["web_app"]`.

**Do not blindly apply a plan that deletes/recreates the existing log group.** Back up the state and inspect the plan first. If the old resource exists at the stated address in the current state, move its state address before applying the refactor:

```bash
terraform state pull > terraform-state-backup.json
terraform state mv \
  'module.monitoring.aws_cloudwatch_log_group.application_logs' \
  'module.logging.aws_cloudwatch_log_group.app_log_groups["web_app"]'
terraform plan
```

Run the `state mv` only if the source address exists in your current state and the destination address is not already managed. If resource addresses differ, inspect `terraform state list` and adapt the move. Keep the state backup out of version control; it can contain sensitive data. Review the plan for other resource replacements because this repository has also changed incident-pipeline resources and Lambda environment configuration.

## Validation sequence

Run locally/in the same Terraform version used by HCP Terraform:

```bash
terraform fmt -recursive
terraform fmt -recursive -check
terraform init
terraform validate
python -m unittest discover -s tests -v
terraform plan
```

To syntax-check the rendered user-data template, replace `${app_archive_url}` and `${cloudwatch_agent_parameter_name}` with test values and convert `$${...}` escapes back to `${...}` before running `bash -n`. The shell syntax check does not test package installation, application startup, IAM permissions, or AWS APIs. If Terraform is managed by HCP Terraform, run plan first and review every change before apply.

## End-to-end test evidence

Test one signal at a time and retain: the native target-group CloudWatch alarm history, the per-instance `SelfHealing/TargetHealth` alarm history, SNS delivery, dispatcher logs, DynamoDB incident item (including `cloudwatch_alarm_name` and `cloudwatch_alarm_status`), worker logs, ALB target health/ASG membership, verification result, Slack message, and DLQ state. Confirm `InstanceId` survives from the CPU alarm's `Trigger.Dimensions` through the incident item. Do not claim automatic CPU/memory/disk/application remediation until an explicitly safe policy and signal-specific verification have been implemented and tested.


## Incident and replacement Slack notifications

The notification pipeline separates incident evidence, alarm state, and lifecycle updates:

- The target-specific EventBridge `ELB Target Health State Change` route persists the affected instance ID and incident, creates a per-incident CloudWatch alarm, persists the alarm-to-incident mapping, publishes the custom metric, and dispatches the remediation worker. It does not send the target-specific detection Slack message directly.
- A separate EventBridge route listens for CloudWatch Alarm State Change events. It correlates the per-incident alarm name with the full incident ID and sends the detection Slack message only after that alarm enters `ALARM`. Failed Slack delivery is recorded and raised for EventBridge retry; the rule has retry policy and a dedicated SQS dead-letter queue with a CloudWatch alarm when messages accumulate.
- The CloudWatch group-level ALB alarm SNS route sends a deduplicated, **notification-only** fallback alert after the native alarm enters `ALARM`. It does not invoke the worker.

Auto Scaling lifecycle events are persisted first, then represented by a one-shot custom CloudWatch alarm. The router sends lifecycle Slack notifications only after the matching CloudWatch Alarm State Change event confirms `ALARM`. The SNS dispatcher does not send Slack directly; EventBridge's CloudWatch state-change route owns ALARM and OK notifications for all environment-prefixed alarms. Lifecycle launch completion is operational evidence only, not proof of recovery. The worker's final message remains the recovery verdict and must be based on target health, application health, and capacity verification. EventBridge delivery is at-least-once; DynamoDB status/conditional writes suppress duplicates, but Slack webhooks cannot provide strict exactly-once delivery if a process crashes after Slack accepts a message but before DynamoDB records success.

After applying this revision, verify `/aws/lambda/<env>-incident-dispatcher`, `/aws/lambda/<env>-incident-event-router`, and `/aws/lambda/<env>-remediation-worker`; inspect `incident_alert_status`, `notification_status`, and `notification_error` fields in DynamoDB if Slack is still silent.

### Separate alarm recovery and remediation-success messages

For target-specific ALB incidents, the EventBridge alarm-state route handles both `ALARM` and `OK` transitions for the per-incident CloudWatch alarm. On `ALARM → OK`, the router sends **CloudWatch Alarm Recovered** and explicitly says application recovery verification is pending. That message is not a success claim. The remediation worker separately sends **Remediation Successful — Recovery Verified** only when it has evidence that the original instance is gone, exactly one replacement candidate is healthy in the target group, the healthy-target count meets the configured capacity requirement, and the configured application HTTP health URL returns 2xx. Configure the URL via the root module's ALB DNS-derived `app_healthcheck_url`; the worker never treats a missing/failed application probe as passed.

The ALB group-level alarm `${env_prefix}-ALB-Unhealthy-Targets` remains a group-scoped, notification-only signal. It cannot reliably identify a single incident or replacement instance. The two-message incident-correlated sequence uses the per-incident alarm name containing the `INC-...` identifier.
