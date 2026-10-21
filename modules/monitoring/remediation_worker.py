import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


# =============================================================================
# LOGGING AND CONFIGURATION
# =============================================================================

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")

INCIDENT_TABLE_NAME = os.environ["INCIDENT_TABLE_NAME"]

TABLE = boto3.resource(
    "dynamodb",
    region_name=REGION,
).Table(INCIDENT_TABLE_NAME)

ELB = boto3.client("elbv2", region_name=REGION)
ASG = boto3.client("autoscaling", region_name=REGION)

# Provided by Terraform from the sensitive HCP Terraform workspace variable.
# This is the webhook URL itself, not a Secrets Manager ARN.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

TARGET_GROUP_ARN = os.environ.get("TARGET_GROUP_ARN", "")
ASG_NAME = os.environ.get("ASG_NAME", "")

MAX_ATTEMPTS = int(os.environ.get("MAX_REMEDIATION_ATTEMPTS", "1"))
VERIFY_TIMEOUT = int(os.environ.get("VERIFICATION_TIMEOUT_SECONDS", "180"))
MIN_HEALTHY_TARGETS = int(os.environ.get("MIN_HEALTHY_TARGETS", "1"))

POLL_INTERVAL_SECONDS = 10
SLACK_TIMEOUT_SECONDS = 5


# =============================================================================
# COMMON HELPERS
# =============================================================================

def _utc_now():
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _update(incident_id, status, **fields):
    """Update an incident record in DynamoDB."""
    expression_names = {"#status": "status"}
    expression_values = {
        ":status": status,
        ":now": _utc_now(),
    }

    assignments = [
        "#status = :status",
        "updated_at = :now",
    ]

    for index, (key, value) in enumerate(fields.items()):
        name_token = f"#field{index}"
        value_token = f":value{index}"

        expression_names[name_token] = key
        expression_values[value_token] = value

        assignments.append(f"{name_token} = {value_token}")

    TABLE.update_item(
        Key={"incident_id": incident_id},
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=expression_names,
        ExpressionAttributeValues=expression_values,
    )


# =============================================================================
# SLACK NOTIFICATIONS
# =============================================================================

def _slack(incident, status, detail):
    """
    Send an incident notification to Slack using the configured webhook.

    The webhook URL is never logged. HTTP and network failures are raised
    to the caller so notification delivery status can be recorded.
    """
    if not SLACK_WEBHOOK_URL:
        raise RuntimeError(
            "SLACK_WEBHOOK_URL is not configured in the Lambda environment."
        )

    message = (
        "*AWS Self-Healing Incident*\n"
        f"*ID:* `{incident.get('incident_id', 'unknown')}`\n"
        f"*Environment:* `{incident.get('environment', 'unknown')}`\n"
        f"*Signal:* `{incident.get('signal_type', 'unknown')}`\n"
        f"*Alarm:* `{incident.get('alarm_name', 'unknown')}`\n"
        f"*Resource:* `{incident.get('instance_id') or 'not instance-scoped'}`\n"
        f"*Outcome:* `{status}`\n"
        f"*Details:* {detail}"
    )

    payload = json.dumps({"text": message}).encode("utf-8")

    request = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=SLACK_TIMEOUT_SECONDS,
        ) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"Slack webhook returned HTTP {response.status}."
                )

    except urllib.error.HTTPError as exc:
        # Do not log the request URL; it contains the webhook credential.
        raise RuntimeError(
            f"Slack webhook returned HTTP {exc.code}."
        ) from None

    except urllib.error.URLError as exc:
        # Avoid logging exception details that could contain sensitive URLs.
        raise RuntimeError(
            f"Slack webhook request failed: {type(exc.reason).__name__}."
        ) from None


# =============================================================================
# AWS HEALTH AND CAPACITY CHECKS
# =============================================================================

def _instance_health(instance_id):
    """Return the ALB target-health state for a particular instance."""
    if not TARGET_GROUP_ARN:
        return None

    response = ELB.describe_target_health(
        TargetGroupArn=TARGET_GROUP_ARN,
        Targets=[{"Id": instance_id}],
    )

    descriptions = response.get("TargetHealthDescriptions", [])

    if not descriptions:
        return "not_registered"

    return descriptions[0].get("TargetHealth", {}).get("State")


def _asg_snapshot():
    """Return the configured Auto Scaling Group or raise an error."""
    if not ASG_NAME:
        raise RuntimeError("ASG_NAME is not configured.")

    response = ASG.describe_auto_scaling_groups(
        AutoScalingGroupNames=[ASG_NAME],
    )

    groups = response.get("AutoScalingGroups", [])

    if not groups:
        raise RuntimeError(
            f"Auto Scaling Group not found: {ASG_NAME}"
        )

    return groups[0]


def _target_counts():
    """
    Return the number of healthy ALB targets and the IDs of unhealthy targets.
    """
    if not TARGET_GROUP_ARN:
        raise RuntimeError(
            "TARGET_GROUP_ARN is required for target-health verification."
        )

    response = ELB.describe_target_health(
        TargetGroupArn=TARGET_GROUP_ARN,
    )

    descriptions = response.get("TargetHealthDescriptions", [])

    healthy = sum(
        1
        for item in descriptions
        if item.get("TargetHealth", {}).get("State") == "healthy"
    )

    unhealthy = [
        item.get("Target", {}).get("Id")
        for item in descriptions
        if item.get("TargetHealth", {}).get("State") == "unhealthy"
    ]

    return healthy, [instance_id for instance_id in unhealthy if instance_id]


# =============================================================================
# BOUNDED REMEDIATION
# =============================================================================

def _remediate(incident):
    """
    Apply a bounded remediation action when an ALB failure is corroborated.

    Other signals are observe-only. This function deliberately does not
    terminate instances for CPU, memory, disk, or application-error alarms.
    """
    signal = incident.get("signal_type", "unknown")
    instance_id = incident.get("instance_id")

    if signal != "alb_health":
        return (
            "OBSERVE_ONLY",
            (
                f"No generic destructive action is permitted for signal "
                f"'{signal}'. The incident remains available for investigation "
                "or a signal-specific remediation policy."
            ),
        )

    if not ASG_NAME or not TARGET_GROUP_ARN:
        return (
            "FAILED_CONFIGURATION",
            "ASG_NAME or TARGET_GROUP_ARN is missing.",
        )

    healthy_targets, unhealthy_targets = _target_counts()

    # Resolve an instance only when there is exactly one unhealthy target.
    # This avoids terminating an arbitrary instance for an ambiguous alarm.
    if not instance_id:
        if len(unhealthy_targets) != 1:
            return (
                "OBSERVE_ONLY",
                (
                    "The incident is not instance-scoped and "
                    f"{len(unhealthy_targets)} unhealthy targets were found. "
                    "No ambiguous automatic action was taken."
                ),
            )

        instance_id = unhealthy_targets[0]
        incident["instance_id"] = instance_id

    # Reconfirm the target state immediately before the destructive action.
    target_state = _instance_health(instance_id)

    if target_state != "unhealthy":
        return (
            "NO_ACTION",
            (
                f"Target health is '{target_state}', not confirmed unhealthy "
                "at decision time."
            ),
        )

    # Preserve the configured minimum service capacity.
    if healthy_targets < MIN_HEALTHY_TARGETS:
        return (
            "BLOCKED_SAFETY_GUARD",
            (
                f"Only {healthy_targets} healthy ALB targets remain; "
                f"the configured minimum is {MIN_HEALTHY_TARGETS}. "
                "No termination was performed."
            ),
        )

    group = _asg_snapshot()

    managed_ids = {
        item.get("InstanceId")
        for item in group.get("Instances", [])
    }

    if instance_id not in managed_ids:
        return (
            "OBSERVE_ONLY",
            (
                f"Unhealthy target {instance_id} is not a member of "
                f"the configured Auto Scaling Group '{ASG_NAME}'."
            ),
        )

    # Request replacement without reducing desired capacity.
    ASG.terminate_instance_in_auto_scaling_group(
        InstanceId=instance_id,
        ShouldDecrementDesiredCapacity=False,
    )

    return (
        "ACTION_REQUESTED",
        (
            f"Requested termination of unhealthy ASG instance {instance_id} "
            "without decrementing desired capacity. The Auto Scaling Group "
            "is expected to launch a replacement; verification is still required."
        ),
    )


# =============================================================================
# RECOVERY VERIFICATION
# =============================================================================

def _verify(incident, initial_status):
    """
    Verify the outcome of a requested remediation.

    ACTION_CONFIRMED means the original instance has left the ASG and the
    configured minimum number of ALB targets is healthy. It does not assert
    application-level correctness beyond those checks.
    """
    if initial_status != "ACTION_REQUESTED":
        return (
            initial_status,
            "No recovery claim: no replacement action was requested.",
        )

    instance_id = incident.get("instance_id")
    deadline = time.monotonic() + VERIFY_TIMEOUT

    while time.monotonic() < deadline:
        try:
            group = _asg_snapshot()

            current_ids = {
                item.get("InstanceId")
                for item in group.get("Instances", [])
            }

            healthy_targets, _ = _target_counts()

            # Do not claim recovery until the original instance has left
            # the ASG and the configured minimum healthy-target count is met.
            if (
                instance_id not in current_ids
                and healthy_targets >= MIN_HEALTHY_TARGETS
            ):
                return (
                    "ACTION_CONFIRMED",
                    (
                        f"Original instance {instance_id} is no longer in "
                        f"the ASG, and the ALB reports {healthy_targets} "
                        f"healthy targets (minimum required: "
                        f"{MIN_HEALTHY_TARGETS})."
                    ),
                )

        except ClientError as exc:
            # Log the AWS error code, not potentially sensitive request data.
            error_code = exc.response.get("Error", {}).get(
                "Code",
                "Unknown",
            )
            LOG.warning(
                "Verification poll failed for incident %s: %s",
                incident.get("incident_id"),
                error_code,
            )

        time.sleep(POLL_INTERVAL_SECONDS)

    return (
        "VERIFICATION_FAILED",
        (
            f"Timed out after {VERIFY_TIMEOUT}s waiting for the original "
            "instance to leave the ASG and the minimum healthy-target "
            "condition to be satisfied."
        ),
    )


# =============================================================================
# LAMBDA HANDLER
# =============================================================================

def lambda_handler(event, context):
    """Process one DynamoDB incident dispatched by the incident dispatcher."""
    incident_id = event.get("incident_id")

    if not incident_id:
        raise ValueError("incident_id is required.")

    result = TABLE.get_item(
        Key={"incident_id": incident_id},
        ConsistentRead=True,
    )

    incident = result.get("Item")

    if not incident:
        raise ValueError(
            f"Incident record not found: {incident_id}"
        )

    # Atomically claim the incident and enforce the attempt limit.
    # Concurrent invocations cannot both claim the same eligible state.
    try:
        TABLE.update_item(
            Key={"incident_id": incident_id},
            UpdateExpression=(
                "SET #status = :processing, "
                "attempt_count = if_not_exists(attempt_count, :zero) + :one, "
                "updated_at = :now"
            ),
            ConditionExpression=(
                "#status IN (:received, :dispatched, :retryable) "
                "AND (attribute_not_exists(attempt_count) "
                "OR attempt_count < :max)"
            ),
            ExpressionAttributeNames={
                "#status": "status",
            },
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":received": "RECEIVED",
                ":dispatched": "DISPATCHED",
                ":retryable": "RETRYABLE",
                ":zero": 0,
                ":one": 1,
                ":max": MAX_ATTEMPTS,
                ":now": _utc_now(),
            },
        )

    except ClientError as exc:
        if (
            exc.response.get("Error", {}).get("Code")
            == "ConditionalCheckFailedException"
        ):
            LOG.warning(
                "Incident already claimed or attempt limit reached: %s",
                incident_id,
            )

            return {
                "status": "SUPPRESSED",
                "incident_id": incident_id,
            }

        raise

    incident["attempt_count"] = (
        int(incident.get("attempt_count", 0)) + 1
    )
    incident["incident_id"] = incident_id

    try:
        action_status, detail = _remediate(incident)

        final_status, verification = _verify(
            incident,
            action_status,
        )

        # Persist the remediation and verification outcomes before notifying
        # Slack. A notification outage must not overwrite the actual outcome.
        _update(
            incident_id,
            final_status,
            affected_instance_id=incident.get("instance_id", ""),
            remediation_result=detail,
            verification_result=verification,
            completed_at=_utc_now(),
        )

        try:
            _slack(
                incident,
                final_status,
                f"{detail}\nVerification: {verification}",
            )

            _update(
                incident_id,
                final_status,
                notification_status="DELIVERED",
            )

        except Exception as notify_exc:
            LOG.exception(
                "Slack notification failed for incident %s",
                incident_id,
            )

            # Notification failure is recorded separately. Do not change a
            # verified remediation outcome to FAILED just because Slack failed.
            _update(
                incident_id,
                final_status,
                notification_status="FAILED",
                notification_error=type(notify_exc).__name__,
            )

        return {
            "status": final_status,
            "incident_id": incident_id,
        }

    except Exception as exc:
        LOG.exception(
            "Incident processing failed: %s",
            incident_id,
        )

        # This covers processing failures. It does not mean every AWS action
        # failed; inspect the persisted incident record and CloudWatch logs.
        _update(
            incident_id,
            "FAILED",
            error_type=type(exc).__name__,
            error_message=str(exc)[:1000],
            completed_at=_utc_now(),
        )

        try:
            _slack(
                incident,
                "FAILED",
                (
                    f"Incident processing failed with "
                    f"{type(exc).__name__}. Inspect the worker's "
                    "CloudWatch logs and incident record."
                ),
            )

            _update(
                incident_id,
                "FAILED",
                notification_status="DELIVERED",
            )

        except Exception:
            LOG.exception(
                "Slack failure notification also failed for incident %s",
                incident_id,
            )

        raise