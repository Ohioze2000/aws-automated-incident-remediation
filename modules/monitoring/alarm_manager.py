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
THRESHOLD = float(os.environ.get("CPU_ALARM_THRESHOLD", "70"))
PERIOD = int(os.environ.get("CPU_ALARM_PERIOD", "120"))
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

    # Scheduled reconciliation creates alarms for current InService instances and removes stale
    # alarms left behind if an EventBridge termination delivery was missed.
    if detail_type == "Scheduled Event" or event.get("source") == "aws.events":
        response = ASG.describe_auto_scaling_groups(AutoScalingGroupNames=[ASG_NAME])
        groups = response.get("AutoScalingGroups", [])
        if not groups:
            raise RuntimeError(f"Auto Scaling Group not found: {ASG_NAME}")
        active = {
            i["InstanceId"]
            for i in groups[0].get("Instances", [])
            if i.get("LifecycleState", "").startswith("InService")
        }
        names = [_create(instance_id) for instance_id in sorted(active)]

        # Remove stale alarms if an EventBridge termination delivery was missed.
        prefix = f"{PREFIX}-EC2-"
        suffix = "-High-CPU"
        paginator = CW.get_paginator("describe_alarms")
        stale = []
        for page in paginator.paginate(AlarmNamePrefix=prefix):
            for alarm in page.get("MetricAlarms", []):
                alarm_name = alarm.get("AlarmName", "")
                if not alarm_name.startswith(prefix) or not alarm_name.endswith(suffix):
                    continue
                instance_id = alarm_name[len(prefix):-len(suffix)]
                if instance_id and instance_id not in active:
                    stale.append(alarm_name)
        for offset in range(0, len(stale), 100):
            CW.delete_alarms(AlarmNames=stale[offset:offset + 100])
        LOG.info("Reconciled %s active CPU alarms and deleted %s stale alarms", len(names), len(stale))
        return {"status": "reconciled", "alarms": names, "deleted_stale_alarms": stale}

    if not instance_id:
        LOG.warning("Lifecycle event has no instance ID: %s", event)
        return {"status": "ignored", "reason": "missing instance id"}
    alarm_name = f"{PREFIX}-EC2-{instance_id}-High-CPU"
    if "terminate" in detail_type.lower() or "terminated" in str(detail.get("LifecycleTransition", "")).lower():
        CW.delete_alarms(AlarmNames=[alarm_name])
        LOG.info("Deleted per-instance alarm %s", alarm_name)
        return {"status": "deleted", "alarm_name": alarm_name}
    return {"status": "created", "alarm_name": _create(instance_id)}
