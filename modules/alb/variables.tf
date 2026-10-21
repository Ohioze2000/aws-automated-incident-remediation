variable "env_prefix" {
  type        = string
  description = "Environment prefix used for resource naming (e.g., dev, staging, prod)"
}

variable "vpc_id" {
  type        = string
  description = "The ID of the VPC where the ALB and Target Group will be deployed"
}

variable "subnet_ids" {
  type        = list(string)
  description = "A list of public subnet IDs where the ALB will be provisioned"
}

variable "certificate_arn" {
  type        = string
  description = "ARN of the ACM certificate for the HTTPS listener. If omitted, the HTTP listener will forward traffic directly on port 80"
  default     = ""
}

variable "health_check_path" {
  type        = string
  description = "Destination path for ALB health check probes"
  default     = "/"
}

variable "tags" {
  type        = map(string)
  description = "A map of tags to assign to all module resources"
  default     = {}
}