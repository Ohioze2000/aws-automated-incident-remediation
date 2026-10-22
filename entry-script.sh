#!/bin/bash
set -Eeuo pipefail

# User data is rendered by Terraform templatefile(). Escape shell parameter
# expansions as $${...}; only app_archive_url and cloudwatch_agent_parameter_name
# below are Terraform template inputs.
exec > >(tee -a /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

on_error() {
  local exit_code=$?
  echo "[ERROR] Bootstrap failed at line $${BASH_LINENO[0]} (exit $${exit_code}); inspect /var/log/user-data.log and /var/log/cloud-init-output.log."
  exit "$${exit_code}"
}
trap on_error ERR

printf '[INFO] Bootstrap started at %s\n' "$(date --iso-8601=seconds)"
if [[ "$(id -u)" -ne 0 ]]; then
  echo "[ERROR] Bootstrap must run as root."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
APP_ARCHIVE_URL="${app_archive_url}"
CLOUDWATCH_AGENT_PARAMETER_NAME="${cloudwatch_agent_parameter_name}"
if [[ -z "$${APP_ARCHIVE_URL}" || -z "$${CLOUDWATCH_AGENT_PARAMETER_NAME}" ]]; then
  echo "[ERROR] app_archive_url and cloudwatch_agent_parameter_name are required."
  exit 1
fi

mkdir -p /var/log/nginx /var/log/app
touch /var/log/nginx/access.log /var/log/nginx/error.log
chmod 0755 /var/log/nginx /var/log/app /var/log/nginx/access.log /var/log/nginx/error.log

# 1. Host prerequisites and Docker Engine.
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-release unzip wget awscli
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc
gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg /tmp/docker.asc
chmod a+r /etc/apt/keyrings/docker.gpg
ARCH="$(dpkg --print-architecture)"
UBUNTU_CODENAME="$(. /etc/os-release && printf '%s' "$${VERSION_CODENAME}")"
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu %s stable\n' "$${ARCH}" "$${UBUNTU_CODENAME}" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker ubuntu || true

# 2. Install and configure the CloudWatch Agent before app deployment so that
# bootstrap/application failures can be investigated from CloudWatch Logs.
IMDS_TOKEN="$(curl --fail --silent --show-error --max-time 5 -X PUT \
  http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600')"
AWS_REGION="$(curl --fail --silent --show-error --max-time 5 \
  -H "X-aws-ec2-metadata-token: $${IMDS_TOKEN}" \
  http://169.254.169.254/latest/meta-data/placement/region)"
if [[ -z "$${AWS_REGION}" ]]; then
  echo "[ERROR] IMDSv2 did not return an AWS region."
  exit 1
fi
export AWS_DEFAULT_REGION="$${AWS_REGION}"

AGENT_DEB=/tmp/amazon-cloudwatch-agent.deb
if ! wget --https-only --tries=3 --timeout=30 -O "$${AGENT_DEB}" \
  "https://s3.$${AWS_REGION}.amazonaws.com/amazoncloudwatch-agent-$${AWS_REGION}/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb"; then
  wget --https-only --tries=3 --timeout=30 -O "$${AGENT_DEB}" \
    https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazoncloudwatch-agent.deb
fi
dpkg -i "$${AGENT_DEB}"
rm -f "$${AGENT_DEB}"
if ! /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 -c "ssm:$${CLOUDWATCH_AGENT_PARAMETER_NAME}" -s; then
  echo "[ERROR] CloudWatch Agent failed to load SSM parameter $${CLOUDWATCH_AGENT_PARAMETER_NAME}."
  aws ssm get-parameter --name "$${CLOUDWATCH_AGENT_PARAMETER_NAME}" --region "$${AWS_REGION}" || true
  exit 1
fi
systemctl enable amazon-cloudwatch-agent
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status || true

# 3. Download and validate the application archive, locate the Dockerfile,
# build from its directory, then verify the local HTTP endpoint.
APP_DIR=/opt/estate-agency
ARCHIVE_PATH="$${APP_DIR}/estate-agency.zip"
SOURCE_DIR="$${APP_DIR}/src"
mkdir -p "$${APP_DIR}"
rm -rf "$${SOURCE_DIR}"
mkdir -p "$${SOURCE_DIR}"
wget --https-only --retry-connrefused --tries=3 --timeout=30 \
  --output-document="$${ARCHIVE_PATH}" "$${APP_ARCHIVE_URL}"
unzip -t "$${ARCHIVE_PATH}"
unzip -q -o "$${ARCHIVE_PATH}" -d "$${SOURCE_DIR}"
DOCKERFILE_PATH="$(find "$${SOURCE_DIR}" -maxdepth 4 -type f -name Dockerfile -print -quit)"
if [[ -z "$${DOCKERFILE_PATH}" ]]; then
  echo "[ERROR] No Dockerfile found in application archive (searched $${SOURCE_DIR})."
  exit 1
fi
BUILD_CONTEXT="$(dirname "$${DOCKERFILE_PATH}")"
docker build --pull -t estate-agency:latest "$${BUILD_CONTEXT}"
docker rm -f estate-agency-app >/dev/null 2>&1 || true
docker run -d \
  --name estate-agency-app \
  --restart unless-stopped \
  -p 80:80 \
  -v /var/log/nginx:/var/log/nginx \
  -v /var/log/app:/var/log/app \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  estate-agency:latest

healthy=0
for attempt in $(seq 1 12); do
  if curl --fail --silent --show-error --max-time 3 http://127.0.0.1/ >/dev/null; then
    healthy=1
    break
  fi
  sleep 5
done
if [[ "$${healthy}" -ne 1 ]]; then
  echo "[ERROR] Application did not return a successful HTTP response after container startup."
  docker ps -a || true
  docker logs --tail 100 estate-agency-app || true
  exit 1
fi

printf '[INFO] Bootstrap completed successfully at %s\n' "$(date --iso-8601=seconds)"
