"""Fast offline checks for the production incident pipeline source contract."""
from pathlib import Path
import ast
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

    def test_worker_uses_conditional_claim_and_attempt_limit(self):
        source = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        self.assertIn("ConditionExpression=", source)
        self.assertIn("MAX_ATTEMPTS", source)
        self.assertIn("VERIFICATION_FAILED", source)

    def test_per_instance_alarm_dimension_and_cleanup(self):
        source = (ROOT / "modules/monitoring/alarm_manager.py").read_text(encoding="utf-8")
        self.assertIn('"Name": "InstanceId"', source)
        self.assertIn("delete_alarms", source)
        self.assertIn("Scheduled Event", source)

    def test_unsafe_signal_defaults_to_observe_only(self):
        source = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        self.assertIn('if signal != "alb_health":', source)
        self.assertIn('"OBSERVE_ONLY"', source)

    def test_slack_webhook_is_loaded_from_secrets_manager(self):
        source = (ROOT / "modules/monitoring/remediation_worker.py").read_text(encoding="utf-8")
        terraform = (ROOT / "modules/monitoring/production_pipeline.tf").read_text(encoding="utf-8")
        self.assertIn("get_secret_value", source)
        self.assertIn("secretsmanager:GetSecretValue", terraform)
        self.assertNotIn("SLACK_WEBHOOK_URL              = var.", terraform)


if __name__ == "__main__":
    unittest.main()
