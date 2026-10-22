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

# Attach AWS-managed policies required for Systems Manager and CloudWatch Agent
resource "aws_iam_role_policy_attachment" "managed_policy_attachments" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
  ])

  role       = aws_iam_role.ec2_ssm_role.name
  policy_arn = each.value
}

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Allow the instance to fetch only the configured CloudWatch Agent parameter.
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
        Resource = "arn:${data.aws_partition.current.partition}:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.cloudwatch_agent_parameter_name}"
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