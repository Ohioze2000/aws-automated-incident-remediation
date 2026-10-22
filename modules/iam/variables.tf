variable "env_prefix" {
  type        = string
  description = "Environment prefix used for resource naming (e.g., dev, staging, prod)"
}

variable "tags" {
  type        = map(string)
  description = "A mapping of tags to assign to all module resources"
  default     = {}
}
variable "cloudwatch_agent_parameter_name" {
  type        = string
  description = "Exact SSM Parameter Store name containing the CloudWatch Agent configuration."
  validation {
    condition     = startswith(var.cloudwatch_agent_parameter_name, "/")
    error_message = "cloudwatch_agent_parameter_name must begin with '/'."
  }
}
