"""Route ELB target-health and ASG lifecycle events into durable incident evidence."""
import hashlib
import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = boto3.resource("dynamodb", region_name=REGION).Table(os.environ["INCIDENT_TABLE_NAME"])
LIFECYCLE = boto3.resource("dynamodb", region_name=REGION).Table(os.environ["LIFECYCLE_TABLE_NAME"])
ASG = boto3.client("autoscaling", region_name=REGION)
CW = boto3.client("cloudwatch", region_name=REGION)
LAM = boto3.client("lambda", region_name=REGION)
WORKER_ARN = os.environ["WORKER_FUNCTION_ARN"]
ASG_NAME = os.environ["ASG_NAME"]
ENV_PREFIX = os.environ.get("ENV_PREFIX", "unknown")
TTL_DAYS = int(os.environ.get("INCIDENT_TTL_DAYS", "90"))
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _iso(value):
    if not value:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def _send_detection_alert(item):
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    message = (
        "*AWS Self-Healing Incident Detected*\n"
        f"*Incident:* `{item['incident_id']}`\n"
        f"*Environment:* `{item['environment']}`\n"
        f"*Signal:* `{item['signal_type']}`\n"
        f"*Original instance:* `{item['original_instance_id']}`\n"
        f"*Target group:* `{item['target_group_arn']}`\n"
        f"*CloudWatch alarm:* `{item.get('cloudwatch_alarm_name', 'unknown')}`\n"
        f"*CloudWatch state:* `ALARM` confirmed at `{item.get('cloudwatch_alarm_state_at', 'unknown')}`\n"
        f"*Reason:* {item['reason']}\n"
        "*Status:* Alarm-confirmed incident; remediation and recovery verification are separate and still pending."
    )
    request = urllib.request.Request(SLACK_WEBHOOK_URL, data=json.dumps({"text": message}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def _send_lifecycle_alert(item):
    """Notify on ASG lifecycle changes without claiming that launch equals recovery."""
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    event_type = item.get("event_type", "ASG lifecycle event")
    instance_id = item.get("instance_id", "unknown")
    if "Launch Successful" in event_type:
        headline = "AWS Auto Scaling Instance Launch Completed"
        meaning = (
            "A new instance has launched. It may be the replacement for an unhealthy instance, "
            "but recovery is not confirmed until the target is healthy and capacity checks pass."
        )
    elif "Terminate Successful" in event_type:
        headline = "AWS Auto Scaling Instance Termination Completed"
        meaning = "The instance termination completed; this event alone does not prove service recovery."
    elif "Unsuccessful" in event_type:
        headline = "AWS Auto Scaling Lifecycle Operation Failed"
        meaning = "Auto Scaling reported an unsuccessful lifecycle operation. Investigate the ASG activity history."
    else:
        headline = "AWS Auto Scaling Lifecycle Event"
        meaning = "Review the ASG lifecycle event and incident evidence."
    message = (
        f"*{headline}*\n"
        f"*Environment:* `{ENV_PREFIX}`\n"
        f"*Auto Scaling Group:* `{item.get('asg_name', ASG_NAME)}`\n"
        f"*Event:* `{event_type}`\n"
        f"*Instance:* `{instance_id}`\n"
        f"*Event time:* `{item.get('event_time', 'unknown')}`\n"
        f"*Region:* `{REGION}`\n"
        f"*Interpretation:* {meaning}"
    )
    request = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps({"text": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")


def _asg_snapshot_ids():
    response = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        raise RuntimeError(f"Auto Scaling Group not found: {ASG_NAME}")
    return sorted(item["InstanceId"] for item in groups[0].get("Instances", []) if item.get("InstanceId"))


def _write_lifecycle(event):
    detail = event.get("detail", {})
    event_type = event.get("detail-type", "unknown")
    group = detail.get("AutoScalingGroupName")
    instance_id = detail.get("EC2InstanceId") or detail.get("InstanceId")
    if group != ASG_NAME or not instance_id:
        LOG.info("Ignoring unrelated/incomplete ASG lifecycle event type=%s group=%s", event_type, group)
        return {"status": "ignored"}
    event_time = _iso(event.get("time"))
    event_id = event.get("id", "no-event-id")
    event_key = f"{event_time}#{event_id}"
    ttl = int(datetime.now(timezone.utc).timestamp()) + TTL_DAYS * 86400
    item = {
        "asg_name": group,
        "event_key": event_key,
        "event_time": event_time,
        "event_id": event_id,
        "event_type": event_type,
        "instance_id": instance_id,
        "detail": json.dumps(detail, separators=(",", ":"))[:3000],
        "notification_status": "PENDING",
        "ttl": ttl,
    }
    is_new = True
    try:
        LIFECYCLE.put_item(Item=item, ConditionExpression="attribute_not_exists(event_key)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        is_new = False
        existing = LIFECYCLE.get_item(
            Key={"asg_name": group, "event_key": event_key}, ConsistentRead=True
        ).get("Item", {})
        if existing.get("notification_status") == "DELIVERED":
            LOG.info("Duplicate ASG lifecycle event suppressed event_key=%s", event_key)
            return {"status": "duplicate", "event_type": event_type, "instance_id": instance_id}
        item = existing or item

    # Lifecycle messages are operational evidence, not a claim of successful recovery.
    # If Slack fails, mark the record and raise so EventBridge can retry delivery.
    try:
        _send_lifecycle_alert(item)
        LIFECYCLE.update_item(
            Key={"asg_name": group, "event_key": event_key},
            UpdateExpression="SET notification_status = :s, notification_sent_at = :t",
            ExpressionAttributeValues={":s": "DELIVERED", ":t": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as notify_exc:
        LOG.exception("ASG lifecycle Slack alert failed event_key=%s", event_key)
        LIFECYCLE.update_item(
            Key={"asg_name": group, "event_key": event_key},
            UpdateExpression="SET notification_status = :s, notification_error = :e",
            ExpressionAttributeValues={":s": "FAILED", ":e": type(notify_exc).__name__},
        )
        raise

    LOG.info(
        "Recorded ASG lifecycle event type=%s instance_id=%s event_time=%s new=%s",
        event_type, instance_id, event_time, is_new,
    )
    return {"status": "recorded" if is_new else "retried", "event_type": event_type, "instance_id": instance_id}


def _ensure_instance_unhealthy_alarm(instance_id, target_group_arn, incident_id, event_time, reason):
    """Create the per-incident alarm, but do not publish its metric yet.

    The caller persists the alarm-to-incident correlation before publishing the
    trigger metric. This ordering prevents a fast CloudWatch state-change event
    from arriving before the durable incident mapping exists.
    """
    alarm_name = f"{ENV_PREFIX}-{instance_id}-Unhealthy-Target-{incident_id}"
    dimensions = [
        {"Name": "Environment", "Value": ENV_PREFIX},
        {"Name": "TargetGroupArn", "Value": target_group_arn},
        {"Name": "InstanceId", "Value": instance_id},
        {"Name": "IncidentId", "Value": incident_id},
    ]
    CW.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=(
            f"Target-specific unhealthy incident for instance {instance_id}; "
            f"target group {target_group_arn}; incident {incident_id}; reason: {reason}. "
            "The incident alert is emitted only after this alarm enters ALARM."
        )[:1024],
        ActionsEnabled=False,
        MetricName="UnhealthyTargetDetected",
        Namespace="SelfHealing/TargetHealth",
        Statistic="Sum",
        Dimensions=dimensions,
        Period=60,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
        Threshold=1,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching",
    )
    LOG.info("Created per-incident CloudWatch alarm=%s incident_id=%s", alarm_name, incident_id)
    return alarm_name, dimensions


def _publish_instance_unhealthy_metric(dimensions, incident_id):
    """Publish the event signal after the alarm-to-incident mapping is durable."""
    CW.put_metric_data(
        Namespace="SelfHealing/TargetHealth",
        MetricData=[{
            "MetricName": "UnhealthyTargetDetected",
            "Dimensions": dimensions,
            # Use publish time so delayed event delivery does not place the datapoint
            # outside the alarm's evaluation window. Original event time stays on record.
            "Timestamp": datetime.now(timezone.utc),
            "Value": 1.0,
            "Unit": "Count",
        }],
    )
    LOG.info("Published target-health metric incident_id=%s", incident_id)


def _send_alarm_recovered_alert(item, alarm_name, state_at):
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
    message = (
        "🟢 *CloudWatch Alarm Recovered*\n"
        f"*Alarm:* `{alarm_name}`\n"
        "*State:* `ALARM → OK`\n"
        f"*Incident:* `{item['incident_id']}`\n"
        f"*State-change time:* `{state_at}`\n"
        "*Status:* The monitored alarm condition has cleared. Application recovery verification is pending."
    )
    request = urllib.request.Request(SLACK_WEBHOOK_URL, data=json.dumps({"text": message}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Slack webhook returned HTTP {response.status}")



def _maybe_send_verified_success(incident_id):
    """Send success only after CloudWatch has confirmed OK and all recovery checks passed."""
    response = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True)
    item = response.get("Item")
    if not item:
        raise RuntimeError(f"No incident record found for verified-success notification: {incident_id}")

    checks_passed = (
        item.get("status") == "RECOVERY_VERIFIED"
        and item.get("target_health_check") == "PASSED"
        and item.get("application_health_check") == "PASSED"
        and item.get("capacity_check") == "PASSED"
        and bool(item.get("replacement_instance_id"))
    )
    cloudwatch_confirmed = (
        item.get("cloudwatch_alarm_state") == "OK"
        and item.get("alarm_recovery_notification_status") == "DELIVERED"
    )
    if not checks_passed or not cloudwatch_confirmed:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET verified_success_notification_status = :pending, updated_at = :at",
            ExpressionAttributeValues={":pending": "PENDING_CLOUDWATCH_OK", ":at": datetime.now(timezone.utc).isoformat()},
        )
        LOG.info(
            "Verified success notification deferred incident_id=%s checks_passed=%s cloudwatch_confirmed=%s",
            incident_id, checks_passed, cloudwatch_confirmed,
        )
        return {"status": "success_notification_deferred", "incident_id": incident_id}

    try:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET verified_success_notification_status = :claimed, verified_success_notification_claimed_at = :at",
            ConditionExpression=(
                "attribute_not_exists(verified_success_notification_status) "
                "OR verified_success_notification_status = :failed "
                "OR verified_success_notification_status = :pending"
            ),
            ExpressionAttributeValues={
                ":claimed": "CLAIMED",
                ":failed": "FAILED",
                ":pending": "PENDING_CLOUDWATCH_OK",
                ":at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            latest = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item", {})
            if latest.get("verified_success_notification_status") == "DELIVERED":
                return {"status": "verified_success_already_notified", "incident_id": incident_id}
            raise RuntimeError(f"Verified-success notification claim is active; retry incident_id={incident_id}")
        raise

    message = (
        "✅ *Remediation Successful — Recovery Verified*\n"
        f"*Incident:* `{incident_id}`\n"
        f"*Replacement instance:* `{item['replacement_instance_id']}`\n"
        "*Target health:* Healthy\n"
        "*Application health:* Passed\n"
        "*Capacity verification:* Passed\n"
        "*CloudWatch alarm:* `ALARM → OK` confirmed\n"
        "*Outcome:* All configured recovery checks passed.\n"
        f"*Evidence:* {item.get('verification_result', 'All configured checks passed.')}"
    )
    try:
        if not SLACK_WEBHOOK_URL:
            raise RuntimeError("SLACK_WEBHOOK_URL is not configured")
        request = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=json.dumps({"text": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Slack webhook returned HTTP {response.status}")
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET verified_success_notification_status = :delivered, verified_success_notified_at = :at",
            ExpressionAttributeValues={":delivered": "DELIVERED", ":at": datetime.now(timezone.utc).isoformat()},
        )
    except Exception as exc:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET verified_success_notification_status = :failed, verified_success_notification_error = :error",
            ExpressionAttributeValues={":failed": "FAILED", ":error": type(exc).__name__},
        )
        LOG.exception("Verified-success Slack notification failed incident_id=%s", incident_id)
        raise
    return {"status": "verified_success_notified", "incident_id": incident_id}

def _alarm_state_changed(event):
    """Correlate ALARM/OK transitions, suppress stale events, and notify each transition separately."""
    detail = event.get("detail", {})
    alarm_name = detail.get("alarmName", "")
    state = (detail.get("state") or {}).get("value", "")
    state_at = _iso((detail.get("state") or {}).get("timestamp") or event.get("time"))
    match = re.search(r"-Unhealthy-Target-(INC-[A-F0-9]{24})$", alarm_name)
    if state not in {"ALARM", "OK"} or not match:
        LOG.info("Ignoring non-target-incident alarm state event alarm=%s state=%s", alarm_name, state)
        return {"status": "ignored"}

    incident_id = match.group(1)
    item = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item")
    if not item:
        raise RuntimeError(f"No incident record found for alarm {alarm_name} / {incident_id}")

    # EventBridge delivery is at-least-once and can be out of order. Never allow an
    # older state transition to overwrite a newer state already persisted.
    previous_at = item.get("cloudwatch_alarm_state_at", "")
    if previous_at and previous_at > state_at:
        LOG.info("Ignoring stale alarm transition incident_id=%s state=%s at=%s newer=%s", incident_id, state, state_at, previous_at)
        return {"status": "stale_event_ignored", "incident_id": incident_id, "alarm_name": alarm_name}

    if state == "ALARM":
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET cloudwatch_alarm_state = :state, cloudwatch_alarm_state_at = :at, cloudwatch_alarm_name = :alarm",
            ExpressionAttributeValues={":state": state, ":at": state_at, ":alarm": alarm_name},
        )
        status = item.get("incident_alert_status", "")
        if status == "DELIVERED":
            return {"status": "already_notified", "incident_id": incident_id, "alarm_name": alarm_name}
        # An OK transition is not allowed to race ahead of the detection notification.
        # EventBridge retries until the ALARM message has been delivered.
        if item.get("alarm_recovery_notification_status") == "DELIVERED":
            LOG.warning("ALARM event arrived after recorded recovery notification; not sending stale detection alert incident_id=%s", incident_id)
            return {"status": "recovery_already_notified", "incident_id": incident_id}
        now = datetime.now(timezone.utc)
        claimed_at = item.get("incident_alert_claimed_at", "")
        stale_claim = False
        if status == "CLAIMED":
            try:
                stale_claim = not claimed_at or (now - datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))).total_seconds() > 120
            except ValueError:
                stale_claim = True
        if status == "CLAIMED" and not stale_claim:
            raise RuntimeError(f"Incident notification claim is active; retry incident_id={incident_id}")
        claim_values = {":claimed": "CLAIMED", ":failed": "FAILED", ":at": now.isoformat(), ":alarm": alarm_name}
        if status == "CLAIMED" and stale_claim:
            claim_condition = "incident_alert_status = :claimed AND incident_alert_claimed_at = :old_claimed_at"
            claim_values[":old_claimed_at"] = claimed_at
        else:
            claim_condition = "attribute_not_exists(incident_alert_status) OR incident_alert_status = :failed"
        try:
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET incident_alert_status = :claimed, incident_alert_claimed_at = :at, cloudwatch_alarm_name = :alarm",
                ConditionExpression=claim_condition,
                ExpressionAttributeValues=claim_values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                latest = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item", {})
                if latest.get("incident_alert_status") == "DELIVERED":
                    return {"status": "already_notified", "incident_id": incident_id}
                raise RuntimeError(f"Concurrent incident notification claim; retry incident_id={incident_id}")
            raise
        item = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item", item)
        item["cloudwatch_alarm_name"] = alarm_name
        item["cloudwatch_alarm_state_at"] = state_at
        try:
            _send_detection_alert(item)
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET incident_alert_status = :delivered, incident_alerted_at = :at, cloudwatch_alarm_state = :state, cloudwatch_alarm_state_at = :state_at",
                ExpressionAttributeValues={":delivered": "DELIVERED", ":at": now.isoformat(), ":state": "ALARM", ":state_at": state_at},
            )
        except Exception as exc:
            LOG.exception("Alarm-gated Slack notification failed incident_id=%s", incident_id)
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET incident_alert_status = :failed, incident_alert_error = :error",
                ExpressionAttributeValues={":failed": "FAILED", ":error": type(exc).__name__},
            )
            raise
        return {"status": "notified", "incident_id": incident_id, "alarm_name": alarm_name}

    # Recovery alert is distinct from worker success: an OK alarm says only that the
    # monitored condition cleared. The worker must still verify replacement, app, and capacity.
    if item.get("incident_alert_status") != "DELIVERED":
        raise RuntimeError(f"Alarm recovery arrived before incident detection delivery; retry incident_id={incident_id}")
    if item.get("alarm_recovery_notification_status") == "DELIVERED":
        success = _maybe_send_verified_success(incident_id)
        return {"status": "recovery_already_notified", "incident_id": incident_id, "alarm_name": alarm_name, "verified_success": success.get("status")}
    try:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET alarm_recovery_notification_status = :claimed, alarm_recovery_notification_claimed_at = :at, cloudwatch_alarm_state = :state, cloudwatch_alarm_state_at = :state_at, cloudwatch_alarm_name = :alarm",
            ConditionExpression="attribute_not_exists(alarm_recovery_notification_status) OR alarm_recovery_notification_status = :failed",
            ExpressionAttributeValues={":claimed": "CLAIMED", ":failed": "FAILED", ":at": datetime.now(timezone.utc).isoformat(), ":state": "OK", ":state_at": state_at, ":alarm": alarm_name},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            latest = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item", {})
            if latest.get("alarm_recovery_notification_status") == "DELIVERED":
                return {"status": "recovery_already_notified", "incident_id": incident_id}
            raise RuntimeError(f"Recovery notification is already claimed; retry incident_id={incident_id}")
        raise
    try:
        _send_alarm_recovered_alert(item, alarm_name, state_at)
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET alarm_recovery_notification_status = :delivered, alarm_recovered_at = :at, cloudwatch_alarm_state = :state, cloudwatch_alarm_state_at = :state_at",
            ExpressionAttributeValues={":delivered": "DELIVERED", ":at": datetime.now(timezone.utc).isoformat(), ":state": "OK", ":state_at": state_at},
        )
    except Exception as exc:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET alarm_recovery_notification_status = :failed, alarm_recovery_notification_error = :error",
            ExpressionAttributeValues={":failed": "FAILED", ":error": type(exc).__name__},
        )
        LOG.exception("CloudWatch alarm recovery Slack notification failed incident_id=%s", incident_id)
        raise
    success = _maybe_send_verified_success(incident_id)
    return {"status": "alarm_recovered_notified", "incident_id": incident_id, "alarm_name": alarm_name, "verified_success": success.get("status")}


def _target_unhealthy(event):
    detail = event.get("detail", {})
    target_group_arn = detail.get("target-group-arn") or detail.get("targetGroupArn")
    target = detail.get("target", {}) or {}
    instance_id = target.get("id") or target.get("Id")
    health = detail.get("target-health", {}) or detail.get("targetHealth", {})
    state = str(health.get("state", health.get("State", ""))).lower()
    if state != "unhealthy" or not target_group_arn or not instance_id or not str(instance_id).startswith("i-"):
        LOG.info("Ignoring target-health event state=%s target_group=%s target=%s", state, target_group_arn, instance_id)
        return {"status": "ignored"}

    event_time = _iso(event.get("time"))
    event_id = event.get("id", "no-event-id")
    incident_id = "INC-" + hashlib.sha256(
        f"ELB|{target_group_arn}|{instance_id}|{event_time}|{event_id}".encode()
    ).hexdigest()[:24].upper()
    reason = health.get("reason", health.get("Reason", "unknown"))
    description = health.get("description", health.get("Description", ""))
    item = {
        "incident_id": incident_id,
        "event_id": event_id,
        "event_source": "eventbridge:elb-target-health",
        "alarm_name": "ELB Target Health State Change",
        "target_group_arn": target_group_arn,
        "signal_type": "alb_health",
        "instance_id": instance_id,
        "original_instance_id": instance_id,
        "state_change_time": event_time,
        "region": event.get("region", REGION),
        "reason": f"ELB target became unhealthy: {reason}. {description}".strip(),
        "environment": ENV_PREFIX,
        # Set DISPATCHED before async invoke to avoid the router overwriting PROCESSING
        # if the worker starts before the router returns.
        "status": "DISPATCHED",
        "recovery_status": "PENDING",
        "remediation_initiator": "undetermined",
        "attempt_count": 0,
        "asg_name": ASG_NAME,
        "asg_instance_ids_at_detection": _asg_snapshot_ids(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ttl": int(datetime.now(timezone.utc).timestamp()) + TTL_DAYS * 86400,
    }
    is_retry = False
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(incident_id)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = TABLE.get_item(Key={"incident_id": incident_id}, ConsistentRead=True).get("Item", {})
        if existing.get("status") != "DISPATCH_FAILED":
            # Retry only when the incident record shows alarm creation did not finish.
            # A normal duplicate must not republish another custom-metric datapoint.
            if existing.get("cloudwatch_alarm_status") == "ALARM_PUBLISHED":
                LOG.info("Duplicate target-health event suppressed incident_id=%s status=%s", incident_id, existing.get("status", "missing"))
                return {"status": "duplicate", "incident_id": incident_id, "cloudwatch_alarm_name": existing.get("cloudwatch_alarm_name", "")}
            alarm_name, dimensions = _ensure_instance_unhealthy_alarm(
                instance_id, target_group_arn, incident_id, event_time,
                f"ELB target became unhealthy: {reason}. {description}".strip(),
            )
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET cloudwatch_alarm_name = :n, cloudwatch_alarm_status = :s REMOVE cloudwatch_alarm_error",
                ExpressionAttributeValues={":n": alarm_name, ":s": "ALARM_CREATED"},
            )
            _publish_instance_unhealthy_metric(dimensions, incident_id)
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET cloudwatch_alarm_status = :s",
                ExpressionAttributeValues={":s": "ALARM_PUBLISHED"},
            )
            LOG.info("Duplicate target-health event reconciled CloudWatch alarm incident_id=%s", incident_id)
            return {"status": "duplicate_alarm_reconciled", "incident_id": incident_id, "cloudwatch_alarm_name": alarm_name}
        try:
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET #s = :s, updated_at = :t REMOVE dispatch_error",
                ConditionExpression="#s = :failed",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "DISPATCHED", ":failed": "DISPATCH_FAILED", ":t": datetime.now(timezone.utc).isoformat()},
            )
            is_retry = True
        except ClientError as claim_exc:
            if claim_exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return {"status": "duplicate", "incident_id": incident_id}
            raise

    cloudwatch_alarm_error = None
    try:
        cloudwatch_alarm_name, dimensions = _ensure_instance_unhealthy_alarm(
            instance_id, target_group_arn, incident_id, event_time,
            f"ELB target became unhealthy: {reason}. {description}".strip(),
        )
        item["cloudwatch_alarm_name"] = cloudwatch_alarm_name
        item["cloudwatch_alarm_status"] = "ALARM_CREATED"
        # Persist correlation before publishing the metric that can trigger an alarm event.
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET cloudwatch_alarm_name = :n, cloudwatch_alarm_status = :s REMOVE cloudwatch_alarm_error",
            ExpressionAttributeValues={":n": cloudwatch_alarm_name, ":s": "ALARM_CREATED"},
        )
        _publish_instance_unhealthy_metric(dimensions, incident_id)
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET cloudwatch_alarm_status = :s",
            ExpressionAttributeValues={":s": "ALARM_PUBLISHED"},
        )
    except Exception as alarm_exc:
        cloudwatch_alarm_error = alarm_exc
        item["cloudwatch_alarm_status"] = "FAILED"
        LOG.exception("CloudWatch per-instance alarm creation/publication failed incident_id=%s", incident_id)
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET cloudwatch_alarm_status = :s, cloudwatch_alarm_error = :e",
            ExpressionAttributeValues={":s": "FAILED", ":e": type(alarm_exc).__name__},
        )

    # Target-specific detection Slack is deliberately NOT sent here. It is emitted
    # only by _alarm_state_changed after the correlated CloudWatch alarm enters ALARM.

    # Dispatch only after the durable incident/alarm correlation and metric publication
    # attempt. Detection notification is handled asynchronously by the alarm-state route.
    # DISPATCHED is written before invoking asynchronously so a fast worker cannot
    # have the router overwrite PROCESSING.
    try:
        LAM.invoke(
            FunctionName=WORKER_ARN,
            InvocationType="Event",
            Payload=json.dumps({"incident_id": incident_id}).encode("utf-8"),
        )
    except Exception as exc:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression="SET #s = :s, dispatch_error = :e, updated_at = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "DISPATCH_FAILED", ":e": type(exc).__name__, ":t": datetime.now(timezone.utc).isoformat()},
        )
        raise
    if cloudwatch_alarm_error is not None:
        # Worker dispatch has been attempted. Returning an error asks EventBridge
        # to retry alarm creation/publication; duplicate handling avoids a second
        # worker dispatch unless the durable record explicitly says dispatch failed.
        raise RuntimeError("CloudWatch per-instance alarm creation failed; EventBridge retry requested") from cloudwatch_alarm_error
    LOG.info("Target-health incident %s and dispatched incident_id=%s original_instance_id=%s cloudwatch_alarm=%s", "retried" if is_retry else "persisted", incident_id, instance_id, cloudwatch_alarm_name)
    return {"status": "retried" if is_retry else "accepted", "incident_id": incident_id, "instance_id": instance_id, "cloudwatch_alarm_name": cloudwatch_alarm_name}


def lambda_handler(event, context):
    detail_type = event.get("detail-type", "")
    source = event.get("source", "")
    if source == "aws.elasticloadbalancing" and detail_type == "ELB Target Health State Change":
        return _target_unhealthy(event)
    if source == "aws.cloudwatch" and detail_type == "CloudWatch Alarm State Change":
        return _alarm_state_changed(event)
    if source == "aws.self-healing" and detail_type == "Remediation Verification Completed":
        incident_id = (event.get("detail") or {}).get("incident_id")
        if not incident_id:
            raise ValueError("Remediation Verification Completed event requires detail.incident_id")
        return _maybe_send_verified_success(incident_id)
    if source == "aws.autoscaling" and detail_type in {
        "EC2 Instance Launch Successful",
        "EC2 Instance Terminate Successful",
        "EC2 Instance Launch Unsuccessful",
        "EC2 Instance Terminate Unsuccessful",
    }:
        return _write_lifecycle(event)
    LOG.info("Ignoring unsupported event source=%s detail_type=%s", source, detail_type)
    return {"status": "ignored"}
