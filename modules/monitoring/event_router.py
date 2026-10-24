"""Route ELB target-health and ASG lifecycle events into durable incident evidence."""
import hashlib
import json
import logging
import os
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
        f"*Reason:* {item['reason']}\n"
        "*Status:* Target-specific incident persisted; safety evaluation and recovery verification are pending."
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
        f"*{headline}*\\n"
        f"*Environment:* `{ENV_PREFIX}`\\n"
        f"*Auto Scaling Group:* `{item.get('asg_name', ASG_NAME)}`\\n"
        f"*Event:* `{event_type}`\\n"
        f"*Instance:* `{instance_id}`\\n"
        f"*Event time:* `{item.get('event_time', 'unknown')}`\\n"
        f"*Region:* `{REGION}`\\n"
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
            LOG.info("Duplicate target-health event suppressed incident_id=%s status=%s", incident_id, existing.get("status", "missing"))
            return {"status": "duplicate", "incident_id": incident_id}
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

    # Notify after persisting the incident but before worker dispatch. This way a
    # dispatch failure cannot suppress the detection message. On a retry, avoid
    # sending a duplicate detection alert if delivery was already recorded.
    existing_alert_status = ""
    if is_retry:
        existing_alert_status = TABLE.get_item(
            Key={"incident_id": incident_id}, ConsistentRead=True
        ).get("Item", {}).get("incident_alert_status", "")
    if existing_alert_status != "DELIVERED":
        try:
            _send_detection_alert(item)
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET incident_alert_status = :s, incident_alerted_at = :t",
                ExpressionAttributeValues={":s": "DELIVERED", ":t": datetime.now(timezone.utc).isoformat()},
            )
        except Exception as notify_exc:
            LOG.exception("Target-health detection Slack alert failed incident_id=%s", incident_id)
            TABLE.update_item(
                Key={"incident_id": incident_id},
                UpdateExpression="SET incident_alert_status = :s, incident_alert_error = :e",
                ExpressionAttributeValues={":s": "FAILED", ":e": type(notify_exc).__name__},
            )

    # Dispatch only after the durable record and detection notification attempt.
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
    LOG.info("Target-health incident %s and dispatched incident_id=%s original_instance_id=%s", "retried" if is_retry else "persisted", incident_id, instance_id)
    return {"status": "retried" if is_retry else "accepted", "incident_id": incident_id, "instance_id": instance_id}


def lambda_handler(event, context):
    detail_type = event.get("detail-type", "")
    source = event.get("source", "")
    if source == "aws.elasticloadbalancing" and detail_type == "ELB Target Health State Change":
        return _target_unhealthy(event)
    if source == "aws.autoscaling" and detail_type in {
        "EC2 Instance Launch Successful",
        "EC2 Instance Terminate Successful",
        "EC2 Instance Launch Unsuccessful",
        "EC2 Instance Terminate Unsuccessful",
    }:
        return _write_lifecycle(event)
    LOG.info("Ignoring unsupported event source=%s detail_type=%s", source, detail_type)
    return {"status": "ignored"}
