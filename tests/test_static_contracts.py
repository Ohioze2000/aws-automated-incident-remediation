"""Offline source-contract checks for the Terraform incident pipeline."""
from pathlib import Path
import ast
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IncidentPipelineContractTests(unittest.TestCase):
    def test_lambda_sources_parse(self):
        for source in (
            ROOT / "modules/monitoring/dispatcher.py",
            ROOT / "modules/monitoring/remediation_worker.py",
            ROOT / "modules/monitoring/alarm_manager.py",
            ROOT / "modules/monitoring/event_router.py",
            ROOT / "validate_infra.py",
        ):
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    def test_alb_cloudwatch_alarm_has_notification_fallback_without_worker_dispatch(self):
        dispatcher = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("_handle_alb_alarm_notification(alarm_name, alarm_arn, state_time, message)", dispatcher)
        self.assertIn('"ALARM_NOTIFICATION_ONLY"', dispatcher)
        self.assertIn("notification_only", dispatcher)
        self.assertIn("Target-specific EventBridge events remain authoritative", dispatcher)

    def test_detection_notification_precedes_worker_dispatch(self):
        dispatcher = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        router = (ROOT / "modules/monitoring/event_router.py").read_text(encoding="utf-8")
        dispatcher_handler = dispatcher[dispatcher.index("def lambda_handler(event, context):"):]
        self.assertLess(dispatcher_handler.index("_send_incident_alert(item)"), dispatcher_handler.rindex("lam.invoke("))
        target_router = router[router.index("def _target_unhealthy(event):"):router.index("def lambda_handler(event, context):")]
        self.assertLess(target_router.index("_send_detection_alert(item)"), target_router.index("LAM.invoke("))

    def test_asg_lifecycle_events_notify_slack_and_retry_failed_delivery(self):
        router = (ROOT / "modules/monitoring/event_router.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn("def _send_lifecycle_alert(item):", router)
        self.assertIn("AWS Auto Scaling Instance Launch Completed", router)
        self.assertIn('notification_status = :s', router)
        self.assertIn('"dynamodb:GetItem", "dynamodb:UpdateItem"', pipeline)
        self.assertIn("raise", router[router.index("def _write_lifecycle(event):"):router.index("def _target_unhealthy(event):")])

    def test_dynamodb_idempotency_is_present(self):
        source = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("attribute_not_exists(incident_id)", source)
        self.assertIn("StateChangeTime", source)

    def test_static_unhealthy_target_alarm_is_exported_and_checked_after_deploy(self):
        root_outputs = (ROOT / "output.tf").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/aws-automated-incident-remediation.yml").read_text(encoding="utf-8")
        monitoring = (ROOT / "modules/monitoring/main.tf").read_text(encoding="utf-8")
        self.assertIn('output "unhealthy_alb_alarm_name"', root_outputs)
        self.assertIn('${var.env_prefix}-ALB-Unhealthy-Targets', root_outputs)
        self.assertIn('resource "aws_cloudwatch_metric_alarm" "unhealthy_alb_alarm"', monitoring)
        self.assertIn("Verify CloudWatch unhealthy-target alarm exists", workflow)
        self.assertIn("describe-alarms", workflow)
        self.assertIn("AWS/ApplicationELB", workflow)
        self.assertIn("UnHealthyHostCount", workflow)

    def test_target_health_event_creates_reviewable_per_instance_cloudwatch_alarm(self):
        router = (ROOT / "modules/monitoring/event_router.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn("def _ensure_instance_unhealthy_alarm(", router)
        self.assertIn('Namespace="SelfHealing/TargetHealth"', router)
        self.assertIn('MetricName="UnhealthyTargetDetected"', router)
        self.assertIn('"Name": "InstanceId", "Value": instance_id', router)
        self.assertIn('"Name": "IncidentId", "Value": incident_id', router)
        self.assertIn("CW.put_metric_alarm(", router)
        self.assertIn("CW.put_metric_data(", router)
        self.assertIn('cloudwatch_alarm_status = :s', router)
        self.assertIn('"cloudwatch:PutMetricAlarm"', pipeline)
        self.assertIn('"cloudwatch:PutMetricData"', pipeline)
        # The EventBridge path sends the detection Slack alert; the per-incident
        # alarm is a review surface, not a second SNS notification source.
        self.assertIn("ActionsEnabled=False", router)

    def test_target_health_event_carries_original_instance_into_dynamodb(self):
        router = (ROOT / "modules/monitoring/event_router.py").read_text(encoding="utf-8")
        terraform = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn('"ELB Target Health State Change"', router)
        self.assertIn('"original_instance_id": instance_id', router)
        self.assertIn('"asg_instance_ids_at_detection": _asg_snapshot_ids()', router)
        self.assertIn('"target-health"    = { state = ["unhealthy"] }', terraform)
        self.assertIn('"${var.env_prefix}-asg-lifecycle-events"', terraform)

    def test_alb_alarm_does_not_create_duplicate_group_scoped_incident(self):
        dispatcher = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        self.assertIn('if _signal(alarm_name) == "alb_health":', dispatcher)
        self.assertIn("_handle_alb_alarm_notification(alarm_name, alarm_arn, state_time, message)", dispatcher)
        self.assertIn("Target-specific EventBridge events remain authoritative", dispatcher)

    def test_worker_correlates_native_asg_replacement_and_verifies_replacement_target(self):
        worker = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        terraform = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn('"REPLACEMENT_ALREADY_STARTED"', worker)
        self.assertIn('"replacement_instance_id"', worker)
        self.assertIn('"RECOVERY_VERIFIED"', worker)
        self.assertIn('"dynamodb:Query"', terraform)
        self.assertIn('LIFECYCLE_TABLE_NAME', terraform)

    def test_slack_outcome_includes_original_replacement_and_initiator(self):
        worker = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        router = (ROOT / "modules/monitoring/event_router.py").read_text(encoding="utf-8")
        self.assertIn("*Original instance:*", worker)
        self.assertIn("*Replacement instance:*", worker)
        self.assertIn("*Remediation initiator:*", worker)
        self.assertIn("_send_detection_alert(item)", router)

    def test_worker_claim_is_bounded_and_verified(self):
        source = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        self.assertIn("ConditionExpression=", source)
        self.assertIn("MAX_ATTEMPTS", source)
        self.assertIn("RECOVERY_FAILED", source)
        self.assertIn('if signal != "alb_health":', source)
        self.assertIn('"OBSERVE_ONLY"', source)

    def test_per_instance_cpu_alarm_and_stale_cleanup(self):
        source = (ROOT / "modules/monitoring/alarm_manager.py").read_text(encoding="utf-8")
        self.assertIn('"Name": "InstanceId"', source)
        self.assertIn("delete_alarms", source)
        self.assertIn("describe_alarms", source)
        self.assertIn("Scheduled Event", source)

    def test_slack_webhook_is_direct_sensitive_environment_input(self):
        worker = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        dispatcher = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        terraform = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("SLACK_WEBHOOK_URL", "")', worker)
        self.assertIn('os.environ.get("SLACK_WEBHOOK_URL", "")', dispatcher)
        self.assertIn("SLACK_WEBHOOK_URL", terraform)
        self.assertNotIn("secretsmanager:GetSecretValue", terraform)
        self.assertNotIn("SLACK_WEBHOOK_SECRET_ARN", worker + dispatcher + terraform)

    def test_application_timeout_alarms_are_classified_as_application_errors(self):
        source = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        self.assertIn('"timeout"', source)
        self.assertIn('"db"', source)
        self.assertIn('return "application_error"', source)

    def test_harmonized_application_metric_filters_and_alarms(self):
        root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        logging_variables = (ROOT / "modules/logging/variables.tf").read_text(encoding="utf-8")
        for signal in ("ApplicationErrors", "HTTP5xxCount", "DBConnectionTimeoutCount"):
            self.assertIn(signal, root_variables)
        self.assertIn('log_group_key    = "web_app"', root_variables)
        self.assertIn('log_group_key    = "nginx/access"', root_variables)
        self.assertIn('default_value = optional(number)', root_variables)
        self.assertIn('default_value    = optional(number)', logging_variables)
        self.assertIn('metric_namespace = "CustomAppMetrics"', root_variables)

    def test_logging_module_is_single_owner(self):
        root_main = (ROOT / "main.tf").read_text(encoding="utf-8")
        monitoring = (ROOT / "modules/monitoring/main.tf").read_text(encoding="utf-8")
        self.assertIn("log_groups           = local.effective_log_groups", root_main)
        self.assertIn("metric_filters       = var.metric_filters", root_main)
        self.assertIn('"/aws/${var.env_prefix}/web_app"', monitoring)
        self.assertNotIn('resource "aws_cloudwatch_log_group" "application_logs"', monitoring)
        self.assertNotIn('resource "aws_cloudwatch_log_metric_filter" "app_error_filter"', monitoring)

    def test_rendered_bootstrap_has_valid_shell_syntax(self):
        source = (ROOT / "entry-script.sh").read_text(encoding="utf-8")
        source = source.replace("${app_archive_url}", "https://example.invalid/app.zip")
        source = source.replace("${cloudwatch_agent_parameter_name}", "/example/cloudwatch-agent")
        source = source.replace("$${", "${")
        result = subprocess.run(["bash", "-n"], input=source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_alarm_manager_has_reconciliation_permissions(self):
        source = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        policy_start = source.index('resource "aws_iam_role_policy" "alarm_manager_policy"')
        policy_end = source.index('resource "aws_cloudwatch_log_group" "alarm_manager_logs"', policy_start)
        policy = source[policy_start:policy_end]
        self.assertIn("cloudwatch:DescribeAlarms", policy)
        self.assertIn("cloudwatch:PutMetricAlarm", policy)
        self.assertIn("cloudwatch:DeleteAlarms", policy)

    def test_dispatch_failed_incidents_can_be_retried_idempotently(self):
        dispatcher = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        pipeline = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn('existing.get("status") != "DISPATCH_FAILED"', dispatcher)
        self.assertIn('ConditionExpression="#s = :failed"', dispatcher)
        self.assertIn('"dynamodb:GetItem"', pipeline)

    def test_lambda_verification_window_fits_worker_timeout(self):
        pipeline = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        variables = (ROOT / "modules/monitoring/variables.tf").read_text(encoding="utf-8")
        self.assertIn("timeout          = 600", pipeline)
        self.assertIn("default     = 420", variables)
        self.assertIn("<= 540", variables)

    def test_audit_report_states_unvalidated_deployment_limitations(self):
        report = (ROOT / "REFACTOR_NOTES.md").read_text(encoding="utf-8")
        self.assertIn("Terraform CLI validation/formatting and live AWS integration tests: not run", report)
        self.assertIn("terraform validate", report)
        self.assertIn("recorded and reported without destructive action", report)

    def test_custom_metric_flag_is_not_advertised_when_unused(self):
        root_variables = (ROOT / "variables.tf").read_text(encoding="utf-8")
        logging_variables = (ROOT / "modules/logging/variables.tf").read_text(encoding="utf-8")
        self.assertNotIn("is_custom_metric", root_variables + logging_variables)

    def test_destroy_workflow_does_not_announce_success_before_destroy(self):
        workflow = (ROOT / ".github/workflows/destroy.yml").read_text(encoding="utf-8")
        self.assertIn("terraform apply -auto-approve destroy.tfplan", workflow)
        self.assertIn("terraform plan -destroy -out=destroy.tfplan", workflow)
        self.assertNotIn("Started Infrastructure Teardown Successfully", workflow)
        self.assertIn("environment: production", workflow)

    def test_ssl_module_receives_environment_prefix(self):
        root_main = (ROOT / "main.tf").read_text(encoding="utf-8")
        ssl_variables = (ROOT / "modules/ssl/variables.tf").read_text(encoding="utf-8")
        self.assertIn('env_prefix                = var.env_prefix', root_main)
        self.assertIn('variable "env_prefix"', ssl_variables)

    def test_www_certificate_and_ipv6_dns_are_consistent(self):
        root_main = (ROOT / "main.tf").read_text(encoding="utf-8")
        self.assertIn('subject_alternative_names = var.create_www_record ? ["www.${var.domain_name}"] : []', root_main)
        self.assertIn("enable_ipv6  = false", root_main)

    def test_runtime_target_health_wait_allows_instance_bootstrap(self):
        source = (ROOT / "validate_infra.py").read_text(encoding="utf-8")
        self.assertIn("def validate_alb_targets(max_retries: int = 40, delay: int = 15)", source)

    def test_terraform_lockfile_is_not_ignored_and_fmt_is_recursive(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/aws-automated-incident-remediation.yml").read_text(encoding="utf-8")
        self.assertNotIn(".terraform.lock.hcl", gitignore)
        self.assertIn("terraform fmt -check -recursive", workflow)

    def test_parameter_name_shared_across_bootstrap_and_iam(self):
        root_main = (ROOT / "main.tf").read_text(encoding="utf-8")
        iam = (ROOT / "modules/iam/main.tf").read_text(encoding="utf-8")
        webserver = (ROOT / "modules/webserver/main.tf").read_text(encoding="utf-8")
        bootstrap = (ROOT / "entry-script.sh").read_text(encoding="utf-8")
        self.assertIn("cloudwatch_agent_parameter_name = var.cloudwatch_agent_parameter_name", root_main)
        self.assertIn("parameter${var.cloudwatch_agent_parameter_name}", iam)
        self.assertIn("templatefile(", webserver)
        self.assertIn('ssm:$${CLOUDWATCH_AGENT_PARAMETER_NAME}', bootstrap)


if __name__ == "__main__":
    unittest.main()
