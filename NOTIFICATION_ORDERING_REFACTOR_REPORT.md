# Notification ordering refactor report (superseded)

This report described an earlier implementation in which Auto Scaling lifecycle notifications remained independent of CloudWatch alarm state. That behavior did not meet the final requirement and has been superseded by `CLOUDWATCH_SLACK_ORDERING_REFACTOR_REPORT.md`.

The current contract is: incident and recovery Slack messages are triggered only by CloudWatch Alarm State Change events; Auto Scaling launch/termination messages first create and publish a one-shot CloudWatch alarm and are sent to Slack only after that alarm enters `ALARM`; the SNS dispatcher handles remediation dispatch only and does not send Slack.

Use `ALARM_GATED_NOTIFICATION_FLOW.md` for the current flow and `CLOUDWATCH_SLACK_ORDERING_REFACTOR_REPORT.md` for the latest change summary and validation limits. Terraform validation and live AWS/Slack integration testing remain required before deployment.
