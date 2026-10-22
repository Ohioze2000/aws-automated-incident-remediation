"""Bounded remediation worker. Destructive action is limited to corroborated ALB failures."""
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = boto3.resource("dynamodb").Table(os.environ["INCIDENT_TABLE_NAME"])
ELB = boto3.client("elbv2", region_name=REGION)
ASG = boto3.client("autoscaling", region_name=REGION)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


TARGET_GROUP_ARN = os.environ.get("TARGET_GROUP_ARN", "")
ASG_NAME = os.environ.get("ASG_NAME", "")
MAX_ATTEMPTS = int(os.environ.get("MAX_REMEDIATION_ATTEMPTS", "1"))
VERIFY_TIMEOUT = int(os.environ.get("VERIFICATION_TIMEOUT_SECONDS", "180"))
MIN_HEALTHY_TARGETS = int(os.environ.get("MIN_HEALTHY_TARGETS", "1"))


def _update(incident_id, status, **fields):
    names = {"#status": "status"}
    values = {":status": status, ":now": datetime.now(timezone.utc).isoformat()}
    sets = ["#status = :status", "updated_at = :now"]
    for i, (key, value) in enumerate(fields.items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#k{i} = :v{i}")
    TABLE.update_item(Key={"incident_id": incident_id}, UpdateExpression="SET " + ", ".join(sets), ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def _slack(incident, status, detail):
    slack_url = SLACK_WEBHOOK_URL
    if not slack_url:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    text = (f"*AWS Self-Healing Incident*\n*ID:* `{incident.get('incident_id')}`\n"
            f"*Environment:* `{incident.get('environment')}`\n*Signal:* `{incident.get('signal_type')}`\n"
            f"*Alarm:* `{incident.get('alarm_name')}`\n*Resource:* `{incident.get('instance_id') or 'not instance-scoped'}`\n"
            f"*Outcome:* `{status}`\n*Details:* {detail}")
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(slack_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def _instance_health(instance_id):
    if not TARGET_GROUP_ARN:
        return None
    response = ELB.describe_target_health(TargetGroupArn=TARGET_GROUP_ARN, Targets=[{"Id": instance_id}])
    descriptions = response.get("TargetHealthDescriptions", [])
    return descriptions[0].get("TargetHealth", {}).get("State") if descriptions else "not_registered"


def _asg_snapshot():
    response = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        raise RuntimeError(f"Auto Scaling Group not found: {ASG_NAME}")
    return groups[0]


def _target_counts():
    if not TARGET_GROUP_ARN:
        raise RuntimeError("TARGET_GROUP_ARN is required for target health verification")
    response = ELB.describe_target_health(TargetGroupArn=TARGET_GROUP_ARN)
    descriptions = response.get("TargetHealthDescriptions", [])
    healthy = sum(1 for item in descriptions if item.get("TargetHealth", {}).get("State") == "healthy")
    unhealthy = [item.get("Target", {}).get("Id") for item in descriptions if item.get("TargetHealth", {}).get("State") == "unhealthy"]
    return healthy, [item for item in unhealthy if item]


def _remediate(incident):
    signal = incident.get("signal_type", "unknown")
    instance_id = incident.get("instance_id")
    # Production default: a threshold breach alone is not enough evidence to reboot/detach.
    # Only ALB-unhealthy + ASG-managed + spare healthy capacity allows bounded replacement.
    if signal != "alb_health":
        return "OBSERVE_ONLY", f"No generic destructive action is safe for signal '{signal}'. Escalated for service-specific policy."
    if not ASG_NAME or not TARGET_GROUP_ARN:
        return "FAILED_CONFIGURATION", "ASG_NAME or TARGET_GROUP_ARN is missing."
    healthy_targets, unhealthy_targets = _target_counts()
    if not instance_id:
        if len(unhealthy_targets) != 1:
            return "OBSERVE_ONLY", f"Alarm was not instance-scoped and {len(unhealthy_targets)} unhealthy targets were found; no ambiguous automatic action taken."
        instance_id = unhealthy_targets[0]
        incident["instance_id"] = instance_id
    target_state = _instance_health(instance_id)
    if target_state != "unhealthy":
        return "NO_ACTION", f"Target health is '{target_state}', not confirmed unhealthy at decision time."
    if healthy_targets < MIN_HEALTHY_TARGETS:
        return "BLOCKED_SAFETY_GUARD", f"Only {healthy_targets} healthy ALB targets remain; configured minimum is {MIN_HEALTHY_TARGETS}. No termination performed."
    group = _asg_snapshot()
    managed_ids = {item.get("InstanceId") for item in group.get("Instances", [])}
    if instance_id not in managed_ids:
        return "OBSERVE_ONLY", f"Unhealthy target {instance_id} is not a member of the configured Auto Scaling Group."
    ASG.terminate_instance_in_auto_scaling_group(InstanceId=instance_id, ShouldDecrementDesiredCapacity=False)
    return "ACTION_REQUESTED", f"Requested termination of unhealthy ASG instance {instance_id} without decrementing desired capacity; ASG should replace it."


def _verify(incident, initial_status):
    if initial_status != "ACTION_REQUESTED":
        # Do not claim recovery for observe-only, blocked, or failed actions.
        return initial_status, "No recovery claim: no replacement action was taken."
    instance_id = incident.get("instance_id")
    deadline = time.time() + VERIFY_TIMEOUT
    while time.time() < deadline:
        try:
            group = _asg_snapshot()
            current_ids = {item.get("InstanceId") for item in group.get("Instances", [])}
            healthy_targets, _ = _target_counts()
            desired_capacity = int(group.get("DesiredCapacity", 0))
            required_healthy = max(MIN_HEALTHY_TARGETS, desired_capacity)
            if instance_id not in current_ids and healthy_targets >= required_healthy:
                return "ACTION_CONFIRMED", f"Original instance is no longer in the ASG and ALB reports {healthy_targets} healthy targets (required {required_healthy}, based on desired ASG capacity)."
        except ClientError as exc:
            LOG.warning("Verification poll failed: %s", exc)
        time.sleep(10)
    return "VERIFICATION_FAILED", f"Timed out after {VERIFY_TIMEOUT}s waiting for termination/capacity verification."


def lambda_handler(event, context):
    incident_id = event.get("incident_id")
    if not incident_id:
        raise ValueError("incident_id is required")
    result = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True)
    incident = result.get("Item")
    if not incident:
        raise ValueError(f"Incident record not found: {incident_id}")
    # Conditional state transition prevents concurrent worker invocations from both acting.
    try:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :processing, attempt_count = if_not_exists(attempt_count, :zero) + :one, updated_at = :now",
            ConditionExpression="#s IN (:received, :dispatched, :retryable) AND (attribute_not_exists(attempt_count) OR attempt_count < :max)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":processing": "PROCESSING", ":received": "RECEIVED", ":dispatched": "DISPATCHED", ":retryable": "RETRYABLE", ":zero": 0, ":one": 1, ":max": MAX_ATTEMPTS, ":now": datetime.now(timezone.utc).isoformat()},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            LOG.warning("Incident already claimed or attempt limit reached: %s", incident_id)
            return {"status": "SUPPRESSED", "incident_id": incident_id}
        raise
    incident["attempt_count"] = int(incident.get("attempt_count", 0)) + 1
    try:
        action_status, detail = _remediate(incident)
        final_status, verification = _verify(incident, action_status)
        _update(incident_id, final_status, affected_instance_id=incident.get("instance_id", ""), remediation_result=detail, verification_result=verification, completed_at=datetime.now(timezone.utc).isoformat())
        incident["incident_id"] = incident_id
        try:
            _slack(incident, final_status, f"{detail}\nVerification: {verification}")
            _update(incident_id, final_status, notification_status="DELIVERED")
        except Exception as notify_exc:
            # Notification failure must not rewrite a verified remediation outcome as a remediation failure.
            LOG.exception("Slack notification failed for incident %s", incident_id)
            _update(incident_id, final_status, notification_status="FAILED", notification_error=type(notify_exc).__name__)
        return {"status": final_status, "incident_id": incident_id}
    except Exception as exc:
        LOG.exception("Incident processing failed: %s", incident_id)
        _update(incident_id, "FAILED", error_type=type(exc).__name__, error_message=str(exc)[:1000])
        incident["incident_id"] = incident_id
        try:
            _slack(incident, "FAILED", f"Processing failed: {type(exc).__name__}. Inspect CloudWatch logs.")
        except Exception:
            LOG.exception("Slack failure notification also failed")
        raise
