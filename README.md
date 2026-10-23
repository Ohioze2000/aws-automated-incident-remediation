# AWS Self-Healing Infrastructure — Incident Pipeline Refactor

This repository provisions an AWS web tier with Terraform and includes a bounded, event-driven incident pipeline. It is a source refactor, **not a claim that AWS deployment or end-to-end recovery has been tested**. Review Terraform plans and validate in a disposable AWS environment before applying.

## Architecture

- **CPU:** EventBridge ASG lifecycle events call `alarm_manager.py`. It creates one `AWS/EC2` `CPUUtilization` alarm per instance with the `InstanceId` dimension. A 15-minute reconciliation run creates missing alarms and removes stale ones.
- **Memory and disk:** CloudWatch Agent publishes `CWAgent` metrics. Terraform alarms use the ASG roll-up dimensions; disk also matches `path=/` and the configured filesystem type.
- **ALB health:** `AWS/ApplicationELB/UnHealthyHostCount` uses the target-group and load-balancer dimensions.
- **Application errors:** `modules/logging` owns the application log groups, metric filters, and alarms. Filter metric names/namespaces must match the configured alarms.
- **Incident pipeline:** CloudWatch → SNS → dispatcher Lambda → DynamoDB incident record → remediation worker → bounded action/observe-only decision → verification → Slack. Dispatcher and worker failures/DLQ are monitored.
- **Safety:** Only a corroborated unhealthy ALB target can currently trigger the bounded replacement action. CPU, memory, disk, application-error, and unknown alarms are recorded and reported as observe-only; they do not automatically terminate or reboot an instance.

## Main refactor changes

1. `modules/logging` is the single Terraform owner for all `/aws/<environment>/...` log groups, filters, and app alarms. The monitoring module no longer creates a duplicate application log group or a competing generic error metric filter.
2. CloudWatch Agent group names match Terraform-created groups: `cloud-init-output`, `user-data`, `nginx/access`, `nginx/error`, and `web_app`.
3. The SSM parameter name is configurable and shared by the Terraform SSM resource, EC2 IAM policy, and `templatefile()` bootstrap. The IAM policy is scoped to the exact parameter ARN.
4. The EC2 launch template renders `entry-script.sh` with `templatefile()`. The script uses the configured archive URL and parameter name, enables an ERR trap, configures the agent before application deployment, validates the ZIP/Dockerfile, and checks the local HTTP endpoint.
5. Slack notifications use the `slack_webhook_url` sensitive Terraform input. Configure it as a **sensitive HCP Terraform workspace variable**; do not commit the real webhook URL. The URL is passed into Lambda environment variables and therefore remains sensitive state/configuration data accessible to sufficiently privileged administrators.
6. The CPU alarm manager's reconciliation also deletes stale per-instance alarms when lifecycle-event delivery was missed.

## Configuration

- Copy `terraform.tfvars.example` to `terraform.tfvars` and replace every placeholder. The HCP Terraform backend in `backend.tf` currently targets organization `DigitalTech` and workspace `awsSelfHealingInfra`; verify these match the workspace you intend to operate on before running `terraform init`.
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

Test one signal at a time and retain: CloudWatch alarm history, SNS delivery, dispatcher logs, DynamoDB incident item, worker logs, ALB target health/ASG membership, verification result, Slack message, and DLQ state. Confirm `InstanceId` survives from the CPU alarm's `Trigger.Dimensions` through the incident item. Do not claim automatic CPU/memory/disk/application remediation until an explicitly safe policy and signal-specific verification have been implemented and tested.
