# Incident remediation policy and safety contract

## Operating principle

An alarm is evidence of a condition, not automatic permission to change infrastructure. The dispatcher records and classifies an incident. The worker may execute only an explicitly implemented, reviewed policy. Unknown and unsupported signals are observe-only.

## Current implementation boundary

| Signal | Current action policy | Why |
|---|---|---|
| CPU | `OBSERVE_ONLY` | High CPU alone does not prove an instance fault; scaling/replacement needs workload-aware policy and verification. |
| Memory | `OBSERVE_ONLY` | Memory pressure can be caused by workload spikes or leaks; no generic destructive action is safe. |
| Disk | `OBSERVE_ONLY` | Automated deletion is unsafe without application-specific path and file-retention rules. |
| Application errors | `OBSERVE_ONLY` | The correct response depends on whether the cause is deployment, dependency, traffic, or application failure. |
| Unhealthy ALB target | Bounded replacement only after safety checks | Target must be confirmed unhealthy, be an InService member of the configured ASG, not be protected from scale-in, and healthy target count must meet the configured floor before the action. |
| Unknown | `OBSERVE_ONLY` | Unknown signals must never trigger an infrastructure mutation. |

Do not describe CPU, memory, disk, or application-error remediation as implemented until a policy, action, and action-specific verification have been added and tested.

## ALB replacement safety contract

Before termination, the worker must confirm all of the following:

1. The target is currently `unhealthy` in the configured target group.
2. The incident identifies one unambiguous instance. If the alarm is not instance-scoped, exactly one unhealthy target must be found.
3. The instance belongs to the configured Auto Scaling Group.
4. The instance lifecycle is `InService`.
5. The instance is not protected from scale-in.
6. The number of healthy targets meets `MIN_HEALTHY_TARGETS` before termination.
7. Desired capacity is not decremented.

After the action, the worker reports `ACTION_CONFIRMED` only when the original instance is gone from the ASG, a different instance is InService and healthy in the target group, the healthy target floor is met, and InService capacity has returned to desired capacity. A timeout or unmet condition is `VERIFICATION_FAILED`, not success.

## Operational limits

- `MAX_REMEDIATION_ATTEMPTS` is bounded to 1–3 and defaults to 1.
- Unknown and unsupported signals are observe-only.
- Safety-guard failures must stop the action and notify an operator.
- An action or notification API returning success is not evidence of recovery.
- Failed asynchronous Lambda invocations are routed to the configured failure destination; operators must monitor the DLQ alarm and reconcile messages deliberately.
- Do not automatically replay a failed incident without checking whether the original action may already have occurred.

## Before enabling additional automatic actions

For each new signal, document: eligibility criteria, allowlisted action, affected-resource scope, maximum impact, concurrency/cooldown rules, idempotency behaviour, rollback/escalation behaviour, and independent verification. Implement unit tests for allowed and blocked paths and run an isolated integration test before enabling the policy in production.
