terraform {
  required_version = "~> 1.15"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.62"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# ==============================================================================
# LOCAL VARIABLES
# ==============================================================================

locals {
  common_tags = merge(
    var.tags,
    {
      Environment = var.env_prefix
      ManagedBy   = "Terraform"
    }
  )

  # One Terraform owner for every CloudWatch log group. Override retention/KMS
  # values through var.log_groups without duplicating the canonical web_app group.
  default_log_groups = {
    "cloud-init-output" = { retention_in_days = var.default_log_retention_days, kms_key_id = null }
    "user-data"         = { retention_in_days = var.default_log_retention_days, kms_key_id = null }
    "nginx/access"      = { retention_in_days = var.default_log_retention_days, kms_key_id = null }
    "nginx/error"       = { retention_in_days = var.default_log_retention_days, kms_key_id = null }
    "web_app"           = { retention_in_days = var.default_log_retention_days, kms_key_id = null }
  }
  effective_log_groups = merge(local.default_log_groups, var.log_groups)

  # Map ACM Domain Validation Options for Route 53 validation records
  cert_validation_map = {
    for dvo in module.ssl.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
}

# ==============================================================================
# 1. CORE VPC & NETWORKING MODULES
# ==============================================================================

module "network" {
  source         = "./modules/network"
  env_prefix     = var.env_prefix
  az_count       = var.az_count
  vpc_cidr_block = var.vpc_cidr_block
  tags           = local.common_tags
}

# ==============================================================================
# 2. SSL & DNS INFRASTRUCTURE
# ==============================================================================

module "ssl" {
  source      = "./modules/ssl"
  domain_name = var.domain_name
  tags        = local.common_tags
}

module "dns" {
  source       = "./modules/dns"
  domain_name       = var.domain_name
  env_prefix        = var.env_prefix
  create_www_record = var.create_www_record
  alb_dns_name      = module.alb.alb_dns_name
  alb_zone_id  = module.alb.alb_zone_id
  tags         = local.common_tags
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.cert_validation_map

  allow_overwrite = true
  zone_id         = module.dns.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
}

resource "aws_acm_certificate_validation" "cert_validation" {
  certificate_arn         = module.ssl.certificate_arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

# ==============================================================================
# 3. APPLICATION LOAD BALANCER
# ==============================================================================

module "alb" {
  source          = "./modules/alb"
  env_prefix      = var.env_prefix
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.public_subnet_ids
  certificate_arn = module.ssl.certificate_arn
  tags            = local.common_tags

  depends_on = [
    aws_acm_certificate_validation.cert_validation
  ]
}

# ==============================================================================
# 4. IAM SECURITY ROLES
# ==============================================================================

module "iam" {
  source                          = "./modules/iam"
  env_prefix                      = var.env_prefix
  cloudwatch_agent_parameter_name = var.cloudwatch_agent_parameter_name
  tags                            = local.common_tags
}

# ==============================================================================
# 5. COMPUTE & AUTO SCALING TIER
# ==============================================================================

module "webserver" {
  source                          = "./modules/webserver"
  env_prefix                      = var.env_prefix
  vpc_id                          = module.network.vpc_id
  private_subnet_ids              = module.network.private_subnet_ids
  alb_security_group_id           = module.alb.alb_security_group_id
  target_group_arn                = module.alb.target_group_arn
  iam_instance_profile_name       = module.iam.iam_instance_profile_name
  instance_type                   = var.instance_type
  image_name                      = var.image_name
  public_key_content              = var.public_key_content
  app_archive_url                 = var.app_archive_url
  cloudwatch_agent_parameter_name = var.cloudwatch_agent_parameter_name
  desired_capacity                = var.desired_capacity
  min_size                        = var.min_size
  max_size                        = var.max_size
  tags                            = local.common_tags
}

# ==============================================================================
# 6. APPLICATION LOGGING & OBSERVABILITY
# ==============================================================================

module "logging" {
  source                     = "./modules/logging"
  env_prefix                 = var.env_prefix
  default_log_retention_days = var.default_log_retention_days
  # This module is the sole Terraform owner for log groups, filters, and app alarms.
  log_groups           = local.effective_log_groups
  metric_filters       = var.metric_filters
  app_alarms           = var.app_alarms
  alarm_sns_topic_arns = [module.monitoring.cloudwatch_alarms_topic_arn]
  tags                 = local.common_tags
}

# ==============================================================================
# 7. MONITORING & AUTOMATED REMEDIATION
# ==============================================================================

module "monitoring" {
  source                          = "./modules/monitoring"
  env_prefix                      = var.env_prefix
  asg_name                        = module.webserver.asg_name
  target_group_arn                = module.alb.target_group_arn
  target_group_dimension          = element(split(":", module.alb.target_group_arn), 5)
  load_balancer_dimension         = trimprefix(element(split(":", module.alb.alb_arn), 5), "loadbalancer/")
  cloudwatch_agent_parameter_name = var.cloudwatch_agent_parameter_name
  slack_webhook_url               = var.slack_webhook_url
  alert_email                     = var.alert_email
  tags                            = local.common_tags
}

# ==============================================================================
# 8. SSM
# ==============================================================================
# module "ssm" {
#   source               = "./modules/ssm"
#   env_prefix           = var.env_prefix
#   ssm_parameter_name   = "/asg-webserver/cloudwatch-agent-config"
#   cw_agent_config_path = "${path.root}/amazon-cloudwatch-agent.json"
#   tags                 = var.tags
# }