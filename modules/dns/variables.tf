variable "domain_name" {
  type        = string
  description = "The apex domain name registered in Route 53 (e.g., example.com)."
}

variable "alb_dns_name" {
  type        = string
  description = "The DNS name of the Application Load Balancer."
}

variable "alb_zone_id" {
  type        = string
  description = "The Route 53 Hosted Zone ID of the Application Load Balancer."
}

variable "env_prefix" {
  type        = string
  default     = "prod"
  description = "Environment prefix used for resource tracking."
}

variable "create_www_record" {
  type        = bool
  default     = true
  description = "Whether to create a www subdomain alias record."
}

variable "enable_ipv6" {
  type        = bool
  default     = true
  description = "Whether to create AAAA alias records for IPv6 routing."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Standard tags map for consistency across modules."
}