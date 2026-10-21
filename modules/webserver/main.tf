# ==============================================================================
# SECURITY GROUPS
# ==============================================================================

resource "aws_security_group" "ec2_sg" {
  name        = "${var.env_prefix}-ec2-sg"
  description = "Allow ALB traffic to reach EC2 application tier"
  vpc_id      = var.vpc_id

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-ec2-sg"
    }
  )

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_to_ec2_http" {
  security_group_id            = aws_security_group.ec2_sg.id
  description                  = "HTTP traffic from ALB target group"
  from_port                    = 80
  to_port                      = 80
  ip_protocol                  = "tcp"
  referenced_security_group_id = var.alb_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "ec2_allow_all_outbound" {
  security_group_id = aws_security_group.ec2_sg.id
  description       = "Allow all outbound traffic for updates and package installation"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ==============================================================================
# AMIs & KEYS
# ==============================================================================

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = [var.image_name]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_key_pair" "ssh_key" {
  count      = var.public_key_content != "" ? 1 : 0
  key_name   = "${var.env_prefix}-server-key"
  public_key = var.public_key_content

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-server-key"
    }
  )
}

# ==============================================================================
# LAUNCH TEMPLATE
# ==============================================================================

resource "aws_launch_template" "web_server_lt" {
  name_prefix            = "${var.env_prefix}-web-server-"
  image_id               = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = var.public_key_content != "" ? aws_key_pair.ssh_key[0].key_name : null
  vpc_security_group_ids = [aws_security_group.ec2_sg.id]

  iam_instance_profile {
    name = var.iam_instance_profile_name
  }

  # Enforce IMDSv2 for metadata security
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = base64encode(templatefile("${path.root}/${var.user_data_path}", {
    app_archive_url                 = var.app_archive_url
    cloudwatch_agent_parameter_name = var.cloudwatch_agent_parameter_name
  }))

  block_device_mappings {
    device_name = "/dev/sda1"

    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"

    tags = merge(
      var.tags,
      {
        Name = "${var.env_prefix}-asg-node"
        SSM  = "Enabled"
      }
    )
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ==============================================================================
# AUTO SCALING GROUP
# ==============================================================================

resource "aws_autoscaling_group" "web_asg" {
  name                = "${var.env_prefix}-web-asg-"
  vpc_zone_identifier = var.private_subnet_ids
  desired_capacity    = var.desired_capacity
  max_size            = var.max_size
  min_size            = var.min_size

  target_group_arns = [var.target_group_arn]

  launch_template {
    id      = aws_launch_template.web_server_lt.id
    version = aws_launch_template.web_server_lt.latest_version
  }

  health_check_type         = "ELB"
  health_check_grace_period = 300

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 50
      instance_warmup        = 300
    }
  }

  dynamic "tag" {
    for_each = merge(
      var.tags,
      {
        Name = "${var.env_prefix}-asg-node"
      }
    )
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }
}