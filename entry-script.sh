#!/bin/bash
set -euo pipefail

# ==============================================================================
# LOGGING SETUP
# ==============================================================================
# Redirect stdout and stderr to both user-data.log and the system console
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1
echo "======================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting EC2 User Data Bootstrap Process"
echo "======================================================================"

export DEBIAN_FRONTEND=noninteractive

APP_ARCHIVE_URL="${app_archive_url}"
CLOUDWATCH_AGENT_PARAMETER_NAME="${cloudwatch_agent_parameter_name}"

if [[ -z "$${APP_ARCHIVE_URL}" || -z "$${CLOUDWATCH_AGENT_PARAMETER_NAME}" ]]; then
    echo "[ERROR] Required bootstrap configuration is missing."
    exit 1
fi

# ==============================================================================
# 1. PREPARE HOST LOG DIRECTORIES
# ==============================================================================
echo "[INFO] Creating log directories on host machine for container volume mounts..."
sudo mkdir -p /var/log/nginx
sudo mkdir -p /var/log/app
sudo chmod 0755 /var/log/nginx /var/log/app

# ==============================================================================
# 2. SYSTEM UPDATES & DEPENDENCY INSTALLATION
# ==============================================================================
echo "[INFO] Updating package index and installing dependencies..."
sudo apt-get update -y
sudo apt-get install -y \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    unzip \
    wget \
    awscli

# ==============================================================================
# 3. DOCKER ENGINE INSTALLATION
# ==============================================================================
echo "[INFO] Configuring Docker official repository..."
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y

echo "[INFO] Installing Docker components..."
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Grant default Ubuntu user access to Docker daemon
sudo usermod -aG docker ubuntu || true

# ==============================================================================
# 4. APPLICATION CONTAINERIZATION & DEPLOYMENT
# ==============================================================================
echo "[INFO] Fetching web content and building Docker image..."
APP_DIR="/opt/estate-agency"
sudo mkdir -p "$${APP_DIR}"
cd "$${APP_DIR}"

sudo wget --https-only --retry-connrefused --tries=3 --timeout=20 -q "$${APP_ARCHIVE_URL}" -O "$${APP_DIR}/estate-agency.zip"
sudo unzip -o "$${APP_DIR}/estate-agency.zip" -d "$${APP_DIR}/src"
cd "$${APP_DIR}/src"

# Handle optional inner folder structure if zip contains a nested directory
if [ ! -f "Dockerfile" ] && [ -d "estate-agency" ]; then
    cd estate-agency
fi

echo "[INFO] Building Docker application container..."
sudo docker build --pull -t estate-agency .

echo "[INFO] Starting Docker application container with mounted host volumes..."
sudo docker run -d \
  --name estate-agency-app \
  --restart always \
  -p 80:80 \
  -v /var/log/nginx:/var/log/nginx \
  -v /var/log/app:/var/log/app \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  estate-agency

# ==============================================================================
# 5. CLOUDWATCH AGENT INSTALLATION & STARTUP (VIA SSM)
# ==============================================================================
echo "[INFO] Retrieving EC2 metadata and region context via IMDSv2..."
IMDS_TOKEN=$(curl -s -S -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
AWS_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
export AWS_DEFAULT_REGION="$${AWS_REGION}"

echo "[INFO] Installing AWS CloudWatch Agent..."
wget -q "https://s3.$${AWS_REGION}.amazonaws.com/amazoncloudwatch-agent-$${AWS_REGION}/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" -O /tmp/amazon-cloudwatch-agent.deb || \
wget -q https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb -O /tmp/amazon-cloudwatch-agent.deb

sudo dpkg -i /tmp/amazon-cloudwatch-agent.deb
rm -f /tmp/amazon-cloudwatch-agent.deb

echo "[INFO] Fetching CloudWatch agent configuration from SSM Parameter Store ($${CLOUDWATCH_AGENT_PARAMETER_NAME})..."
if sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -c ssm:$${CLOUDWATCH_AGENT_PARAMETER_NAME} \
    -s; then
    echo "[INFO] CloudWatch Agent configured and started successfully."
    sudo systemctl enable amazon-cloudwatch-agent
else
    echo "[ERROR] Failed to fetch CloudWatch Agent config from SSM Parameter Store!"
    echo "[DIAGNOSTIC] Testing SSM permission via AWS CLI..."
    aws ssm get-parameter --name "$${CLOUDWATCH_AGENT_PARAMETER_NAME}" --region "$${AWS_REGION}" || true
    exit 1
fi

echo "[INFO] Verifying CloudWatch Agent runtime status..."
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status

echo "======================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Instance bootstrap complete!"
echo "======================================================================"