"""Reconcile per-instance CPU alarms from ASG lifecycle events and a periodic sweep."""
import logging
import os
import boto3

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)
CW = boto3.client("cloudwatch")
ASG = boto3.client("autoscaling")
PREFIX = os.environ["ALARM_PREFIX"]
ASG_NAME = os.environ["ASG_NAME"]
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
THRESHOLD = float(os.environ.get("CPU_ALARM_THRESHOLD", "80"))
PERIOD = int(os.environ.get("CPU_ALARM_PERIOD", "300"))
EVALUATION_PERIODS = int(os.environ.get("CPU_EVALUATION_PERIODS", "2"))
DATAPOINTS = int(os.environ.get("CPU_DATAPOINTS_TO_ALARM", "2"))


def _create(instance_id):
    alarm_name = f"{PREFIX}-EC2-{instance_id}-High-CPU"
    CW.put_metric_alarm(
        AlarmName=alarm_name,
        AlarmDescription=f"Per-instance CPU alarm for {instance_id}; threshold {THRESHOLD}%.",
        ActionsEnabled=True,
        MetricName="CPUUtilization",
        Namespace="AWS/EC2",
        Statistic="Average",
        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        Period=PERIOD,
        EvaluationPeriods=EVALUATION_PERIODS,
        DatapointsToAlarm=DATAPOINTS,
        Threshold=THRESHOLD,
        ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="missing",
        AlarmActions=[TOPIC_ARN],
        OKActions=[TOPIC_ARN],
    )
    LOG.info("Created/updated per-instance alarm %s", alarm_name)
    return alarm_name


def lambda_handler(event, context):
    detail = event.get("detail", {})
    detail_type = event.get("detail-type", "")
    instance_id = detail.get("EC2InstanceId") or detail.get("instance-id") or detail.get("InstanceId")

    # Scheduled reconciliation ensures alarms exist for instances already running at deployment,
    # and repairs missed EventBridge deliveries. It does not remove alarms for live instances.
    if detail_type == "Scheduled Event" or event.get("source") == "aws.events":
        response = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
        groups = response.get("AutoScalingGroups", [])
        if not groups:
            raise RuntimeError(f"Auto Scaling Group not found: {ASG_NAME}")
        active = [i["InstanceId"] for i in groups[0].get("Instances", []) if i.get("LifecycleState", "").startswith("InService")]
        names = [_create(i) for i in active]
        LOG.info("Reconciled %s per-instance CPU alarms", len(names))
        return {"status": "reconciled", "alarms": names}

    if not instance_id:
        LOG.warning("Lifecycle event has no instance ID: %s", event)
        return {"status": "ignored", "reason": "missing instance id"}
    alarm_name = f"{PREFIX}-EC2-{instance_id}-High-CPU"
    if "terminate" in detail_type.lower() or "terminated" in str(detail.get("LifecycleTransition", "")).lower():
        CW.delete_alarms(AlarmNames=[alarm_name])
        LOG.info("Deleted per-instance alarm %s", alarm_name)
        return {"status": "deleted", "alarm_name": alarm_name}
    return {"status": "created", "alarm_name": _create(instance_id)}
