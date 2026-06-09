variable "aws_region" {
  description = "AWS region for the developer VPC and resources."
  type        = string
  default     = "us-east-2"
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
  default     = "us-east-2a"
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

variable "manager_subnet_cidr" {
  description = "CIDR for the second public subnet that hosts the developer manager."
  type        = string
  default     = "10.20.2.0/24"
}

variable "manager_instance_type" {
  description = "EC2 instance type for the developer manager. t4g.small = 2 vCPU / 2 GB on Graviton — matches the prog-strength backend's proven Grafana/Prometheus sizing."
  type        = string
  default     = "t4g.small"
}

variable "manager_data_volume_size_gb" {
  description = "Size of the manager's EBS data volume. Holds Prometheus TSDB and Grafana SQLite (and Loki chunks if stretch ships)."
  type        = number
  default     = 20
}

variable "grafana_admin_user" {
  description = "Manager Grafana admin username. Seeded from GitHub repo secrets via TF_VAR_grafana_admin_user; mirrors prog-strength-api's GRAFANA_ADMIN_USER. Empty default lets plan run without the secret set; compose falls through to admin/admin if both stay empty (do not ship to prod with empty)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grafana_admin_password" {
  description = "Manager Grafana admin password. Seeded from GitHub repo secrets via TF_VAR_grafana_admin_password; mirrors prog-strength-api's GRAFANA_ADMIN_PASSWORD."
  type        = string
  sensitive   = true
  default     = ""
}

# sow_path used to template the worker EC2's userdata; the worker EC2
# moved out of Terraform (so concurrent dispatches no longer race the
# state lock), so the variable is no longer needed.
