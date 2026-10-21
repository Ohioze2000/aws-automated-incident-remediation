# ==============================================================================
# IAM ROLE & INSTANCE PROFILE FOR EC2 (SSM & CLOUDWATCH)
# ==============================================================================

resource "aws_iam_role" "ec2_ssm_role" {
  name = "${var.env_prefix}-ec2-ssm-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-ec2-ssm-role"
    }
  )
}

# Attach AWS-managed policies for Systems Manager (SSM), CloudWatch Agent, and CloudWatch Read-Only
resource "aws_iam_role_policy_attachment" "managed_policy_attachments" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
    "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess"
  ])

  role       = aws_iam_role.ec2_ssm_role.name
  policy_arn = each.value
}

# Inline policy allowing the EC2 instance to fetch the CloudWatch Agent configuration
resource "aws_iam_role_policy" "ec2_read_cw_ssm_parameter" {
  name = "${var.env_prefix}-ec2-read-cw-ssm-param"
  role = aws_iam_role.ec2_ssm_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowFetchCWAgentSSMConfig"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = "arn:aws:ssm:*:*:parameter/asg-webserver/cloudwatch-agent-config"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ec2_ssm_profile" {
  name = "${var.env_prefix}-ec2-ssm-profile"
  role = aws_iam_role.ec2_ssm_role.name

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-ec2-ssm-profile"
    }
  )
}