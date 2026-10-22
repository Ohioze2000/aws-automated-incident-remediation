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
            ROOT / "validate_infra.py",
        ):
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    def test_dynamodb_idempotency_is_present(self):
        source = (ROOT / "modules/monitoring/dispatcher.py").read_text(encoding="utf-8")
        self.assertIn("attribute_not_exists(incident_id)", source)
        self.assertIn("StateChangeTime", source)

    def test_worker_claim_is_bounded_and_verified(self):
        source = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        self.assertIn("ConditionExpression=", source)
        self.assertIn("MAX_ATTEMPTS", source)
        self.assertIn("VERIFICATION_FAILED", source)
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
