variable "domain_name" {
  type        = string
  description = "The root domain name for the SSL certificate (e.g., example.com)."
}

variable "env_prefix" {
  type        = string
  default     = "prod"
  description = "Environment prefix used for resource naming."
}

variable "subject_alternative_names" {
  type        = list(string)
  default     = []
  description = "List of SANs to attach to the certificate (e.g. ['www.example.com'] or ['*.example.com'])."
}

variable "validate_certificate" {
  type        = bool
  default     = false
  description = "Whether to wait for ACM certificate validation within this module (requires passing validation_record_fqdns)."
}

variable "validation_record_fqdns" {
  type        = list(string)
  default     = []
  description = "List of FQDNs created by the DNS module used to satisfy ACM validation."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "A map of tags to assign to the SSL resources."
}