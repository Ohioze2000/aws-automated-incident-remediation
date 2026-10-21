import os
import sys
import time
import boto3
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- ENVIRONMENT VARIABLES ---
ALB_ARN = os.getenv("ALB_ARN")
TARGET_GROUP_ARN = os.getenv("TARGET_GROUP_ARN")
EC2_SG_ID = os.getenv("EC2_SECURITY_GROUP_ID")
ALB_DNS = os.getenv("ALB_DNS_NAME")
DOMAIN_NAME = os.getenv("DOMAIN_NAME")
APP_ENDPOINT = os.getenv("APP_ENDPOINT")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
ENV_PREFIX = os.getenv("ENV_PREFIX", "prod")

# --- BOTO3 CLIENTS ---
elbv2_client = boto3.client("elbv2")
ec2_client = boto3.client("ec2")


def send_slack_alert(title: str, status: str, details: str, color: str = "#ff0000"):
    """Sends structured Slack notifications for quality gate failures."""
    if not SLACK_WEBHOOK_URL:
        print("⚠️ Slack Webhook URL missing. Skipping notification.")
        return

    payload = {
        "attachments": [
            {
                "color": color,
                "title": f"🚨 [{ENV_PREFIX.upper()}] Quality Gate Failure: {title}",
                "text": f"*Component Status:* {status}\n*Failure Details:* {details}",
                "footer": "Infrastructure Quality Gate Engine",
            }
        ]
    }
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=5)
        response.raise_for_status()
    except Exception as e:
        print(f"⚠️ Failed to send Slack alert: {e}")


def validate_security_groups() -> bool:
    """Ensures EC2 Security Group does not expose unrestricted public access."""
    print("🕵️‍♂️ Auditing Security Group ingress configurations...")
    if not EC2_SG_ID:
        send_slack_alert("Security Group Error", "FAILED", "EC2_SECURITY_GROUP_ID is missing.")
        return False

    try:
        response = ec2_client.describe_security_groups(GroupIds=[EC2_SG_ID])
        permissions = response["SecurityGroups"][0].get("IpPermissions", [])

        for perm in permissions:
            # Check IPv4 rules
            for pair in perm.get("IpRanges", []):
                if pair.get("CidrIp") == "0.0.0.0/0":
                    send_slack_alert(
                        "Security Group Violation",
                        "FAILED",
                        f"SG `{EC2_SG_ID}` allows unrestricted IPv4 access (0.0.0.0/0).",
                    )
                    return False

            # Check IPv6 rules
            for v6_pair in perm.get("Ipv6Ranges", []):
                if v6_pair.get("CidrIpv6") == "::/0":
                    send_slack_alert(
                        "Security Group Violation",
                        "FAILED",
                        f"SG `{EC2_SG_ID}` allows unrestricted IPv6 access (::/0).",
                    )
                    return False

        print("✅ Security Group configuration verified (no open 0.0.0.0/0 or ::/0).")
        return True

    except Exception as e:
        send_slack_alert("Security Group Audit Exception", "FAILED", str(e))
        return False


def validate_alb_targets(max_retries: int = 20, delay: int = 15) -> bool:
    """Polls Target Group until all targets achieve a healthy state."""
    print("🏥 Checking Target Group health status...")
    if not TARGET_GROUP_ARN:
        send_slack_alert("Target Group Error", "FAILED", "TARGET_GROUP_ARN is missing.")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            response = elbv2_client.describe_target_health(TargetGroupArn=TARGET_GROUP_ARN)
            health_descriptions = response.get("TargetHealthDescriptions", [])

            # FIX: Allow retries if the pool is initially empty (e.g., during ASG warmup)
            if not health_descriptions:
                print(f"⏳ [Attempt {attempt}/{max_retries}]: No targets registered yet. Retrying in {delay}s...")
                time.sleep(delay)
                continue

            unhealthy_targets = [
                t for t in health_descriptions if t["TargetHealth"]["State"] != "healthy"
            ]

            if not unhealthy_targets:
                print(f"✅ Target pool fully healthy. Active targets: {len(health_descriptions)}")
                return True

            states = [t["TargetHealth"]["State"] for t in unhealthy_targets]
            print(
                f"⏳ [Attempt {attempt}/{max_retries}]: Targets converging... Current non-healthy states: {states}. Retrying in {delay}s..."
            )
            time.sleep(delay)

        except Exception as e:
            send_slack_alert("Target Health API Error", "FAILED", str(e))
            return False

    send_slack_alert(
        "Target Group Convergence Timeout",
        "FAILED",
        f"Target group failed to reach healthy state within {max_retries * delay} seconds.",
    )
    return False


def validate_application_readiness(max_retries: int = 6, delay: int = 10) -> bool:
    """Validates HTTP endpoint accessibility and status code response."""
    print("🌐 Checking HTTP/HTTPS application endpoint readiness...")
    headers = {"User-Agent": "Aetheris-Verification-Engine/1.0"}
    verify_ssl = True

    # Protocol & Host resolution priority
    if APP_ENDPOINT:
        url = APP_ENDPOINT if APP_ENDPOINT.startswith("http") else f"https://{APP_ENDPOINT}"
    elif DOMAIN_NAME:
        url = f"https://{DOMAIN_NAME}"
    elif ALB_DNS:
        url = f"http://{ALB_DNS}"  # Standard ALB DNS addresses usually default to HTTP
        verify_ssl = False
    else:
        send_slack_alert("DNS Resolution Error", "FAILED", "No valid application endpoint or URL provided.")
        return False

    print(f"🔗 Target Endpoint: {url}")

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=verify_ssl)
            if response.status_code in [200, 301, 302, 307, 308]:
                print(f"✅ Application HTTP check passed with status code: {response.status_code}")
                return True
            
            print(f"⚠️ [Attempt {attempt}/{max_retries}]: Unexpected status code {response.status_code}. Retrying in {delay}s...")
            time.sleep(delay)

        except Exception as e:
            if attempt == max_retries:
                send_slack_alert("Endpoint Unreachable", "FAILED", f"URL `{url}` failed reachability check: {e}")
                return False
            print(f"⏳ [Attempt {attempt}/{max_retries}]: Connection failed ({e}). Retrying in {delay}s...")
            time.sleep(delay)

    return False


def main():
    print(f"🚀 Starting Quality Gate Verification for Environment: [{ENV_PREFIX.upper()}]")
    
    # Run quality gate checks sequentially
    if not validate_security_groups():
        sys.exit(1)

    if not validate_alb_targets():
        sys.exit(1)

    if not validate_application_readiness():
        sys.exit(1)

    print(f"🎉 All quality gate checks PASSED for [{ENV_PREFIX.upper()}].")
    sys.exit(0)


if __name__ == "__main__":
    main()