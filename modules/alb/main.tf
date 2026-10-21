# ==============================================================================
# SECURITY GROUP & FIREWALL RULES
# ==============================================================================

resource "aws_security_group" "alb_sg" {
  name        = "${var.env_prefix}-alb-sg"
  description = "Allow inbound HTTP and HTTPS traffic to the Application Load Balancer"
  vpc_id      = var.vpc_id

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-alb-sg"
    }
  )

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_ingress" {
  security_group_id = aws_security_group.alb_sg.id
  description       = "Allow inbound HTTP traffic"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https_ingress" {
  security_group_id = aws_security_group.alb_sg.id
  description       = "Allow inbound HTTPS traffic"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_allow_all_egress" {
  security_group_id = aws_security_group.alb_sg.id
  description       = "Allow all outbound traffic from ALB to targets"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ==============================================================================
# APPLICATION LOAD BALANCER
# ==============================================================================

resource "aws_lb" "app_alb" {
  name               = "${var.env_prefix}-app-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = var.subnet_ids

  drop_invalid_header_fields = true

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-app-alb"
    }
  )
}

# ==============================================================================
# TARGET GROUP
# ==============================================================================

resource "aws_lb_target_group" "app_tg" {
  name                 = "${var.env_prefix}-app-tg"
  port                 = 80
  protocol             = "HTTP"
  vpc_id               = var.vpc_id
  target_type          = "instance"
  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = var.health_check_path
    protocol            = "HTTP"
    port                = "traffic-port"
    matcher             = "200,301,302"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = merge(
    var.tags,
    {
      Name = "${var.env_prefix}-app-tg"
    }
  )

  lifecycle {
    create_before_destroy = true
  }
}

# ==============================================================================
# LISTENERS
# ==============================================================================

# HTTP Listener: Forwards to Target Group if no ACM cert provided; otherwise redirects to HTTPS
# HTTP Listener: Always redirects to HTTPS
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.app_alb.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# HTTPS Listener: Created conditionally when an ACM Certificate ARN is passed
# HTTPS Listener: Always created
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.app_alb.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app_tg.arn
  }
}