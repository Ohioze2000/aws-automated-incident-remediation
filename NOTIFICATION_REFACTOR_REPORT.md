# Notification refactor report (superseded)

This document describes an earlier revision that included direct Slack delivery from the SNS dispatcher and direct lifecycle-event Slack notifications. Those paths were removed because they did not guarantee the requested CloudWatch-first ordering.

The current implementation contract is documented in `ALARM_GATED_NOTIFICATION_FLOW.md` and `CLOUDWATCH_SLACK_ORDERING_REFACTOR_REPORT.md`: the EventBridge CloudWatch Alarm State Change route owns incident ALARM/OK and lifecycle Slack notifications; SNS dispatcher only persists/deduplicates incidents and invokes the remediation worker.

The latest source-level test suite should be run with `python -m unittest discover -s tests -v`. Terraform CLI validation, a reviewed HCP Terraform plan, and live AWS/EventBridge/CloudWatch/SNS/Slack tests have not been performed in this environment.
