"""Bounded ALB remediation with ASG lifecycle correlation and evidence-based verification."""
import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
REGION = os.environ.get("AWS_REGION", "us-east-1")
DDB = boto3.resource("dynamodb", region_name=REGION)
TABLE = DDB.Table(os.environ["INCIDENT_TABLE_NAME"])
LIFECYCLE = DDB.Table(os.environ["LIFECYCLE_TABLE_NAME"])
ELB = boto3.client("elbv2", region_name=REGION)
ASG = boto3.client("autoscaling", region_name=REGION)
LAM = boto3.client("lambda", region_name=REGION)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
TARGET_GROUP_ARN = os.environ.get("TARGET_GROUP_ARN", "")
APP_HEALTHCHECK_URL = os.environ.get("APP_HEALTHCHECK_URL", "")
ROUTER_FUNCTION_NAME = os.environ.get("ROUTER_FUNCTION_NAME", "")
ASG_NAME = os.environ.get("ASG_NAME", "")
MAX_ATTEMPTS = int(os.environ.get("MAX_REMEDIATION_ATTEMPTS", "1"))
VERIFY_TIMEOUT = int(os.environ.get("VERIFICATION_TIMEOUT_SECONDS", "420"))
MIN_HEALTHY_TARGETS = int(os.environ.get("MIN_HEALTHY_TARGETS", "1"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _update(incident_id, status, **fields):
    names = {"#status": "status"}
    values = {":status": status, ":now": _now()}
    sets = ["#status = :status", "updated_at = :now"]
    for i, (key, value) in enumerate(fields.items()):
        names[f"#k{i}"] = key
        values[f":v{i}"] = value
        sets.append(f"#k{i} = :v{i}")
    TABLE.update_item(Key={"incident_id": incident_id}, UpdateExpression="SET " + ", ".join(sets), ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def _slack(incident, status, detail, verification):
    """Send non-success outcomes only; verified-success is CloudWatch-gated by event_router."""
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    text = (
        "*AWS Self-Healing Incident Outcome*\n"
        f"*Incident:* `{incident.get('incident_id')}`\n"
        f"*Environment:* `{incident.get('environment')}`\n"
        f"*Signal:* `{incident.get('signal_type')}`\n"
        f"*Original instance:* `{incident.get('original_instance_id') or incident.get('instance_id') or 'unknown'}`\n"
        f"*Replacement instance:* `{incident.get('replacement_instance_id') or 'not confirmed'}`\n"
        f"*Target health check:* `{incident.get('target_health_check', 'NOT_VERIFIED')}`\n"
        f"*Application health check:* `{incident.get('application_health_check', 'NOT_VERIFIED')}`\n"
        f"*Capacity check:* `{incident.get('capacity_check', 'NOT_VERIFIED')}`\n"
        f"*Remediation initiator:* `{incident.get('remediation_initiator', 'undetermined')}`\n"
        f"*Remediation outcome:* `{incident.get('remediation_outcome', status)}`\n"
        f"*Recovery status:* `{incident.get('recovery_status', status)}`\n"
        f"*Final status:* `{status}`\n"
        f"*Details:* {detail}\n*Verification:* {verification}"
    )
    request = urllib.request.Request(SLACK_WEBHOOK_URL, data=json.dumps({"text": text}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def _instance_health(instance_id):
    if not TARGET_GROUP_ARN or not instance_id:
        return None
    response = ELB.describe_target_health(TargetGroupArn=TARGET_GROUP_ARN, Targets=[{"Id": instance_id}])
    descriptions = response.get("TargetHealthDescriptions", [])
    return descriptions[0].get("TargetHealth", {}).get("State") if descriptions else "not_registered"


def _target_counts():
    if not TARGET_GROUP_ARN:
        raise RuntimeError("TARGET_GROUP_ARN is required for target health verification")
    response = ELB.describe_target_health(TargetGroupArn=TARGET_GROUP_ARN)
    descriptions = response.get("TargetHealthDescriptions", [])
    healthy = sum(1 for item in descriptions if item.get("TargetHealth", {}).get("State") == "healthy")
    unhealthy = [item.get("Target", {}).get("Id") for item in descriptions if item.get("TargetHealth", {}).get("State") == "unhealthy"]
    return healthy, [item for item in unhealthy if item]


def _application_health():
    """Perform a real HTTP probe; missing configuration is never reported as passed."""
    if not APP_HEALTHCHECK_URL:
        return False, "APP_HEALTHCHECK_URL is not configured"
    request = urllib.request.Request(APP_HEALTHCHECK_URL, headers={"User-Agent": "aws-incident-remediation-verifier/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            passed = 200 <= response.status < 300
            return passed, f"HTTP {response.status} from configured application health URL"
    except Exception as exc:
        return False, f"Application health probe failed: {type(exc).__name__}"


def _asg_snapshot():
    response = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        raise RuntimeError(f"Auto Scaling Group not found: {ASG_NAME}")
    return groups[0]


def _lifecycle_events(incident, wait_seconds=0):
    """Read lifecycle evidence after the target-health event; retry briefly for EventBridge ordering."""
    group = incident.get("asg_name") or ASG_NAME
    event_time = incident.get("state_change_time") or incident.get("created_at") or _now()
    try:
        start = datetime.fromisoformat(event_time.replace("Z", "+00:00")) - timedelta(seconds=10)
    except ValueError:
        start = datetime.now(timezone.utc) - timedelta(minutes=10)
    start_key = start.astimezone(timezone.utc).isoformat()
    end_key = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat() + "#~"
    deadline = time.time() + wait_seconds
    events = []
    while True:
        response = LIFECYCLE.query(
            KeyConditionExpression=Key("asg_name").eq(group) & Key("event_key").between(start_key, end_key),
            ScanIndexForward=True,
        )
        events = response.get("Items", [])
        if wait_seconds <= 0 or events or time.time() >= deadline:
            return events
        time.sleep(2)


def _remediate(incident):
    signal = incident.get("signal_type", "unknown")
    original_id = incident.get("original_instance_id") or incident.get("instance_id")
    incident["original_instance_id"] = original_id or ""
    incident["remediation_initiator"] = "none"
    incident["remediation_outcome"] = "OBSERVE_ONLY"
    if signal != "alb_health":
        return "OBSERVE_ONLY", f"No generic destructive action is safe for signal '{signal}'."
    if not original_id or not ASG_NAME or not TARGET_GROUP_ARN:
        return "FAILED_CONFIGURATION", "Original instance ID, ASG_NAME, or TARGET_GROUP_ARN is missing."

    group = _asg_snapshot()
    members = {item.get("InstanceId"): item for item in group.get("Instances", []) if item.get("InstanceId")}
    lifecycle = _lifecycle_events(incident, wait_seconds=8)
    original_terminations = [e for e in lifecycle if e.get("instance_id") == original_id and "Terminate" in e.get("event_type", "") and "Successful" in e.get("event_type", "")]
    if original_id not in members:
        incident["remediation_initiator"] = "autoscaling" if original_terminations else "external_or_autoscaling"
        incident["remediation_outcome"] = "REPLACEMENT_ALREADY_STARTED"
        return "REPLACEMENT_ALREADY_STARTED", (
            f"Original instance {original_id} was already absent from the ASG before this worker requested any action; "
            f"termination lifecycle evidence={'confirmed' if original_terminations else 'not yet observed'}. Worker did not terminate it."
        )
    lifecycle_state = str(members[original_id].get("LifecycleState", ""))
    if lifecycle_state.startswith("Terminating"):
        incident["remediation_initiator"] = "autoscaling" if original_terminations else "autoscaling_or_external"
        incident["remediation_outcome"] = "REPLACEMENT_ALREADY_STARTED"
        return "REPLACEMENT_ALREADY_STARTED", f"Original instance is already in ASG lifecycle state {lifecycle_state}; worker did not issue a second termination."

    target_state = _instance_health(original_id)
    if target_state != "unhealthy":
        incident["remediation_outcome"] = "NO_ACTION"
        return "NO_ACTION", f"Target health is '{target_state}', not confirmed unhealthy at decision time; no termination requested."

    healthy_targets, _ = _target_counts()
    if healthy_targets < MIN_HEALTHY_TARGETS:
        incident["remediation_outcome"] = "BLOCKED_SAFETY_GUARD"
        return "BLOCKED_SAFETY_GUARD", f"Only {healthy_targets} healthy ALB targets remain; minimum is {MIN_HEALTHY_TARGETS}. No termination performed."

    # Refresh the ASG immediately before a destructive action to reduce the race with native ASG recovery.
    latest = _asg_snapshot()
    latest_members = {item.get("InstanceId"): item for item in latest.get("Instances", [])}
    if original_id not in latest_members or str(latest_members[original_id].get("LifecycleState", "")).startswith("Terminating"):
        incident["remediation_initiator"] = "autoscaling_or_external"
        incident["remediation_outcome"] = "REPLACEMENT_ALREADY_STARTED"
        return "REPLACEMENT_ALREADY_STARTED", "ASG state changed during the safety check; worker withheld termination and will verify the lifecycle."
    latest_health = _instance_health(original_id)
    if latest_health != "unhealthy":
        incident["remediation_outcome"] = "NO_ACTION"
        return "NO_ACTION", f"Target recovered or changed state to '{latest_health}' during the safety check; no termination requested."
    latest_healthy, _ = _target_counts()
    if latest_healthy < MIN_HEALTHY_TARGETS:
        incident["remediation_outcome"] = "BLOCKED_SAFETY_GUARD"
        return "BLOCKED_SAFETY_GUARD", f"Healthy target count fell to {latest_healthy} during the safety check; no termination performed."

    ASG.terminate_instance_in_auto_scaling_group(InstanceId=original_id, ShouldDecrementDesiredCapacity=False)
    incident["remediation_initiator"] = "worker"
    incident["remediation_outcome"] = "ACTION_REQUESTED"
    return "ACTION_REQUESTED", f"Worker requested bounded termination of unhealthy instance {original_id}; desired ASG capacity was not decremented."


def _verify(incident, initial_status):
    if initial_status not in {"ACTION_REQUESTED", "REPLACEMENT_ALREADY_STARTED"}:
        incident["recovery_status"] = "NOT_APPLICABLE"
        return initial_status, "No recovery claim: no replacement was requested or observed by the worker."

    original_id = incident.get("original_instance_id") or incident.get("instance_id")
    baseline = set(incident.get("asg_instance_ids_at_detection", []))
    deadline = time.time() + VERIFY_TIMEOUT
    last_evidence = "Waiting for ASG lifecycle and ALB health to converge."
    while time.time() < deadline:
        try:
            group = _asg_snapshot()
            members = {item.get("InstanceId"): item for item in group.get("Instances", []) if item.get("InstanceId")}
            current_ids = set(members)
            healthy_targets, _ = _target_counts()
            desired = int(group.get("DesiredCapacity", 0))
            required_healthy = max(MIN_HEALTHY_TARGETS, desired)
            lifecycle = _lifecycle_events(incident)
            terminations = [e for e in lifecycle if e.get("instance_id") == original_id and "Terminate" in e.get("event_type", "") and "Successful" in e.get("event_type", "")]
            launches = [e for e in lifecycle if "Launch Successful" in e.get("event_type", "") and e.get("instance_id") and e.get("instance_id") != original_id]
            added_ids = current_ids - baseline - ({original_id} if original_id else set())
            launch_ids = {e.get("instance_id") for e in launches}
            # Prefer lifecycle launches after the incident time; a replacement can launch
            # before this Lambda snapshots the ASG, so it may already be in the baseline.
            candidates = launch_ids if launch_ids else added_ids
            healthy_replacements = sorted(candidate for candidate in candidates if _instance_health(candidate) == "healthy")
            if original_id not in current_ids and healthy_targets >= required_healthy and len(healthy_replacements) == 1:
                replacement_id = healthy_replacements[0]
                app_passed, app_evidence = _application_health()
                incident["replacement_instance_id"] = replacement_id
                incident["target_health_check"] = "PASSED"
                incident["target_health_evidence"] = f"Replacement {replacement_id} healthy; ALB healthy targets {healthy_targets}/{required_healthy}."
                incident["capacity_check"] = "PASSED"
                incident["capacity_evidence"] = f"ASG desired capacity={desired}; ALB healthy targets={healthy_targets}; required={required_healthy}; original instance absent."
                incident["application_health_check"] = "PASSED" if app_passed else "FAILED"
                incident["application_health_evidence"] = app_evidence
                if app_passed:
                    incident["recovery_status"] = "RECOVERY_VERIFIED"
                    if terminations:
                        incident["remediation_initiator"] = "autoscaling" if incident.get("remediation_initiator") != "worker" else "worker"
                    return "RECOVERY_VERIFIED", (
                        f"Original instance {original_id} is absent; replacement {replacement_id} is healthy; "
                        f"ALB reports {healthy_targets} healthy targets (required {required_healthy}); "
                        f"application probe passed ({app_evidence}); ASG termination event "
                        f"{'observed' if terminations else 'not observed in lifecycle stream'}; "
                        f"initiator={incident.get('remediation_initiator')}."
                    )
                last_evidence = f"Target and capacity checks passed, but application health failed: {app_evidence}"
            if len(healthy_replacements) > 1:
                last_evidence = f"Original is absent={original_id not in current_ids}; multiple new healthy candidates={healthy_replacements}; refusing to guess replacement identity."
            else:
                last_evidence = f"Original present={original_id in current_ids}; candidate replacements={sorted(candidates)}; healthy targets={healthy_targets}/{required_healthy}; healthy replacement candidates={healthy_replacements}; termination lifecycle event={'observed' if terminations else 'not observed'}."
        except ClientError as exc:
            LOG.warning("Verification poll failed: %s", exc)
            last_evidence = f"Verification API error: {exc.response.get('Error', {}).get('Code', 'ClientError')}"
        time.sleep(10)
    incident["recovery_status"] = "RECOVERY_FAILED"
    return "RECOVERY_FAILED", f"Timed out after {VERIFY_TIMEOUT}s without unambiguous replacement verification. Last evidence: {last_evidence}"


def lambda_handler(event, context):
    incident_id = event.get("incident_id")
    if not incident_id:
        raise ValueError("incident_id is required")
    result = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True)
    incident = result.get("Item")
    if not incident:
        raise ValueError(f"Incident record not found: {incident_id}")
    try:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :processing, attempt_count = if_not_exists(attempt_count, :zero) + :one, updated_at = :now",
            ConditionExpression="#s IN (:received, :dispatched, :retryable) AND (attribute_not_exists(attempt_count) OR attempt_count < :max)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":processing": "PROCESSING", ":received": "RECEIVED", ":dispatched": "DISPATCHED", ":retryable": "RETRYABLE", ":zero": 0, ":one": 1, ":max": MAX_ATTEMPTS, ":now": _now()},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            LOG.warning("Incident already claimed or attempt limit reached: %s", incident_id)
            return {"status": "SUPPRESSED", "incident_id": incident_id}
        raise

    incident["incident_id"] = incident_id
    try:
        action_status, detail = _remediate(incident)
        final_status, verification = _verify(incident, action_status)
        incident["recovery_status"] = incident.get("recovery_status", "NOT_APPLICABLE")
        _update(
            incident_id,
            final_status,
            original_instance_id=incident.get("original_instance_id", incident.get("instance_id", "")),
            affected_instance_id=incident.get("original_instance_id", incident.get("instance_id", "")),
            replacement_instance_id=incident.get("replacement_instance_id", ""),
            remediation_initiator=incident.get("remediation_initiator", "undetermined"),
            remediation_outcome=incident.get("remediation_outcome", action_status),
            recovery_status=incident.get("recovery_status", "NOT_APPLICABLE"),
            target_health_check=incident.get("target_health_check", "NOT_VERIFIED"),
            target_health_evidence=incident.get("target_health_evidence", "Not verified"),
            application_health_check=incident.get("application_health_check", "NOT_VERIFIED"),
            application_health_evidence=incident.get("application_health_evidence", "Not verified"),
            capacity_check=incident.get("capacity_check", "NOT_VERIFIED"),
            capacity_evidence=incident.get("capacity_evidence", "Not verified"),
            remediation_result=detail,
            verification_result=verification,
            completed_at=_now(),
        )
        try:
            if final_status == "RECOVERY_VERIFIED":
                # Do not send success directly from the worker. The router releases it
                # only after CloudWatch has emitted ALARM -> OK and the recovery alert
                # is recorded as delivered. This also handles either event ordering.
                if not ROUTER_FUNCTION_NAME:
                    raise RuntimeError("ROUTER_FUNCTION_NAME is not configured")
                LAM.invoke(
                    FunctionName=ROUTER_FUNCTION_NAME,
                    InvocationType="Event",
                    Payload=json.dumps({
                        "source": "aws.self-healing",
                        "detail-type": "Remediation Verification Completed",
                        "detail": {"incident_id": incident_id},
                    }).encode("utf-8"),
                )
                _update(incident_id, final_status, notification_status="WAITING_FOR_CLOUDWATCH_OK")
            else:
                _slack(incident, final_status, detail, verification)
                _update(incident_id, final_status, notification_status="DELIVERED")
        except Exception as notify_exc:
            LOG.exception("Slack/notification handoff failed for incident %s", incident_id)
            _update(incident_id, final_status, notification_status="FAILED", notification_error=type(notify_exc).__name__)
        LOG.info("Incident completed incident_id=%s status=%s initiator=%s original=%s replacement=%s", incident_id, final_status, incident.get("remediation_initiator"), incident.get("original_instance_id"), incident.get("replacement_instance_id"))
        return {"status": final_status, "incident_id": incident_id}
    except Exception as exc:
        LOG.exception("Incident processing failed: %s", incident_id)
        _update(incident_id, "FAILED", recovery_status="RECOVERY_FAILED", error_type=type(exc).__name__, error_message=str(exc)[:1000])
        try:
            _slack(incident, "FAILED", f"Processing failed: {type(exc).__name__}. Inspect CloudWatch logs.", "Recovery could not be verified.")
        except Exception:
            LOG.exception("Slack failure notification also failed")
        raise
