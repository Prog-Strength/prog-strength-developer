variable "aws_region" {
  description = "AWS region for the developer VPC and resources."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the developer VPC. Must not overlap the application VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet that hosts the worker."
  type        = string
  default     = "10.20.1.0/24"
}

variable "availability_zone" {
  description = "AZ for the public subnet. Single AZ is fine for ephemeral compute."
  type        = string
  default     = "us-east-1a"
}

variable "instance_type" {
  description = "EC2 instance type for the worker. t3.large = 2 vCPU / 8 GB."
  type        = string
  default     = "t3.large"
}

variable "max_runtime_hours" {
  description = "Hard backstop. The worker terminates after this many hours regardless of Claude's state."
  type        = number
  default     = 6
}

variable "github_org" {
  description = "GitHub organization that owns the repos the worker will clone and open PRs against."
  type        = string
  default     = "Prog-Strength"
}

variable "github_actions_repo" {
  description = "The repo whose Actions workflow is allowed to assume the GHA OIDC role. Restricts the trust policy."
  type        = string
  default     = "Prog-Strength/prog-strength-developer"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

variable "sow_path" {
  description = "Path to the SOW within prog-strength-docs (e.g. sows/foo.md). Templated into userdata."
  type        = string
  default     = ""
}
