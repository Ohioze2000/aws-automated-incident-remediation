# Refactor notes: safety-first incident remediation

## Changes in this revision

- Added `cloudwatch:DescribeAlarms` to the CPU alarm manager execution policy so scheduled reconciliation can enumerate and clean up stale alarms.
- Added conditional recovery of incidents left in `RECEIVED` or `DISPATCH_FAILED` when SNS redelivers the same alarm notification. The original incident ID and DynamoDB record are retained.
- Validated the asynchronous Lambda invocation response before considering dispatch accepted.
- Tightened ALB replacement eligibility: target must still be unhealthy, belong to the configured ASG, be `InService`, not be protected from scale-in, and the configured healthy-target floor must be met before action.
- Strengthened ALB replacement verification: the original instance must be gone, a different instance must be `InService` and healthy in the target group, the healthy-target floor must be met, and InService capacity must be restored to desired capacity.
- Added an alarm for CPU alarm manager Lambda errors and classify internal pipeline alarms separately from application-error alarms.
- Standardized Slack notifications on the sensitive `slack_webhook_url` Terraform variable. Removed the unused Secrets Manager ARN configuration, runtime retrieval code, and conditional secret-read IAM permissions.
- Added operational policy and incident-response documentation, an example application-error metric filter/alarm configuration, and CI execution of the Python contract tests on pull requests and pushes.

## Important safety boundary retained intentionally

Only corroborated unhealthy ALB targets can currently trigger an automatic infrastructure mutation. CPU, memory, disk, application errors, pipeline-health alarms, and unknown signals are recorded and reported without destructive action. This is intentional: a generic reboot, termination, scale-out, or file cleanup policy for those signals could harm a healthy service or destroy data.

The application-error example is illustrative and is not enabled by default. Match its filter to the actual application log format and validate it against real log events before use.

## Validation performed in this refactor environment

- Python source syntax compilation: passed.
- `python -m unittest discover -s tests -v`: passed (15 tests).
- Terraform CLI validation/formatting and live AWS integration tests: not run in this environment. Run `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`, and a reviewed plan in a non-production workspace before applying.


## Slack incident-notification reliability fixes

- The SNS dispatcher no longer silently discards a CloudWatch ALB alarm. It writes a deduplicated `ALARM_NOTIFICATION_ONLY` record and sends a fallback Slack alert without invoking the worker; target-specific EventBridge events remain authoritative for ALB remediation.
- The dispatcher now attempts the detection notification after persisting the incident and before worker invocation, so a failed asynchronous invoke does not suppress the initial alert. Duplicate SNS deliveries retry a missing/failed alert without starting another worker for an already active/completed incident.
- The EventBridge target-health router now attempts its instance-specific detection alert before invoking the worker and skips a duplicate alert when a retry finds delivery already recorded.
- ASG launch/termination lifecycle events now send Slack operational notifications. Launch messages identify the new instance but explicitly state that launch is not proof of recovery; only the worker's verified outcome may claim recovery.
- Lifecycle notification status is stored in the lifecycle table. Failed sends raise after recording failure so EventBridge can retry. Added the corresponding DynamoDB `GetItem`/`UpdateItem` permissions and tests.

