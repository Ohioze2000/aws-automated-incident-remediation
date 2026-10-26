"""SNS alarm dispatcher: validate, deduplicate, persist, and enqueue incidents."""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
ddb = boto3.resource("dynamodb")
lam = boto3.client("lambda")
TABLE = ddb.Table(os.environ["INCIDENT_TABLE_NAME"])
WORKER_ARN = os.environ["WORKER_FUNCTION_ARN"]
ENV_PREFIX = os.environ.get("ENV_PREFIX", "unknown")


def _signal(alarm_name: str) -> str:
    value = alarm_name.lower()
    if "cpu" in value:
        return "cpu"
    if "memory" in value or "mem" in value:
        return "memory"
    if "disk" in value:
        return "disk"
    if "unhealthy" in value or "alb" in value or "target-health" in value:
        return "alb_health"
    if any(token in value for token in ("app", "error", "5xx", "db", "timeout", "exception")):
        return "application_error"
    return "unknown"


def lambda_handler(event, context):
    records = event.get("Records", [])
    if not records:
        raise ValueError("Expected SNS Records in event")
    for record in records:
        if record.get("EventSource") != "aws:sns" and "Sns" not in record:
            LOG.warning("Skipping non-SNS record")
            continue
        message = json.loads(record["Sns"]["Message"])
        state = message.get("NewStateValue")
        # OK notifications are operational state changes, not remediation requests.
        if state != "ALARM":
            LOG.info("Ignoring alarm state=%s alarm=%s", state, message.get("AlarmName"))
            continue
        alarm_name = message.get("AlarmName")
        state_time = message.get("StateChangeTime")
        alarm_arn = message.get("AlarmArn", alarm_name or "unknown")
        if not alarm_name or not state_time:
            raise ValueError("CloudWatch alarm message lacks AlarmName or StateChangeTime")
        # Group-scoped ALB alarms are notification-only for remediation. Slack is
        # emitted by the CloudWatch Alarm State Change EventBridge route, never SNS.
        if _signal(alarm_name) == "alb_health":
            LOG.info("ALB alarm state=%s alarm=%s; Slack notification is owned by CloudWatch EventBridge route", state, alarm_name)
            continue
        incident_id = "INC-" + hashlib.sha256(f"{alarm_arn}|{state_time}".encode()).hexdigest()[:24].upper()
        dimensions = {
            item.get("name"): item.get("value")
            for item in message.get("Trigger", {}).get("Dimensions", [])
            if item.get("name") and item.get("value")
        }
        item = {
            "incident_id": incident_id,
            "alarm_arn": alarm_arn,
            "alarm_name": alarm_name,
            "state_change_time": state_time,
            "signal_type": _signal(alarm_name),
            "instance_id": dimensions.get("InstanceId", ""),
            "dimensions": dimensions,
            "region": message.get("Region", os.environ.get("AWS_REGION", "us-east-1")),
            "reason": message.get("NewStateReason", "No alarm reason provided"),
            "environment": ENV_PREFIX,
            "status": "RECEIVED",
            "attempt_count": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + int(os.environ.get("INCIDENT_TTL_DAYS", "90")) * 86400,
        }
        try:
            TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(incident_id)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

            # A prior SNS/Lambda delivery may have persisted the incident but failed
            # before the worker accepted the async invocation. Retry only that known
            # failure state; never redispatch completed or actively processing incidents.
            existing = TABLE.get_item(
                Key={"incident_id": incident_id}, ConsistentRead=True
            ).get("Item", {})
            if existing.get("status") != "DISPATCH_FAILED":
                LOG.info("Duplicate alarm delivery suppressed incident_id=%s status=%s", incident_id, existing.get("status", "missing"))
                continue
            try:
                TABLE.update_item(
                    Key={"incident_id": incident_id},
                    UpdateExpression="SET #s = :dispatched, updated_at = :t REMOVE dispatch_error",
                    ConditionExpression="#s = :failed",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":dispatched": "DISPATCHED",
                        ":failed": "DISPATCH_FAILED",
                        ":t": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except ClientError as claim_exc:
                if claim_exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    LOG.info("Dispatch retry already claimed incident_id=%s", incident_id)
                    continue
                raise
            try:
                lam.invoke(
                    FunctionName=WORKER_ARN,
                    InvocationType="Event",
                    Payload=json.dumps({"incident_id": incident_id}).encode("utf-8"),
                )
                LOG.warning("Retried previously failed worker dispatch incident_id=%s", incident_id)
            except Exception:
                TABLE.update_item(
                    Key={"incident_id": incident_id},
                    UpdateExpression="SET #s = :s, dispatch_error = :e, updated_at = :t",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "DISPATCH_FAILED",
                        ":e": "Worker invocation failed during dispatch retry",
                        ":t": datetime.now(timezone.utc).isoformat(),
                    },
                )
                raise
            continue
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :s, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "DISPATCHED", ":t": datetime.now(timezone.utc).isoformat()},
        )
        # Slack notifications are intentionally absent here. The CloudWatch alarm
        # state-change route owns detection and recovery messages.
        try:
            lam.invoke(
                FunctionName=WORKER_ARN,
                InvocationType="Event",
                Payload=json.dumps({"incident_id": incident_id}).encode("utf-8"),
            )
        except Exception:
            # Keep the idempotency record and expose dispatch failure for operator reconciliation.
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET #s = :s, dispatch_error = :e, updated_at = :t",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "DISPATCH_FAILED", ":e": "Worker invocation failed", ":t": datetime.now(timezone.utc).isoformat()},
            )
            raise
        LOG.info("Incident dispatched incident_id=%s signal=%s", incident_id, item["signal_type"])
    return {"status": "accepted"}
