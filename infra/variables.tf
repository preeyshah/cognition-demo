variable "name" {
  type    = string
  default = "refunds-console"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image_tag" {
  description = "Git SHA of the backend image in ECR (set by the deploy workflow)."
  type        = string
  default     = "latest"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "devin_org_id" {
  description = "Initial Devin org id, stored with the API key in the devin-api-key secret (later updates: scripts/secrets.sh push)."
  type        = string
  default     = ""
}

variable "devin_max_acu" {
  description = "ACU cap per auto-remediation Devin session."
  type        = number
  default     = 10
}

variable "github_repo" {
  type    = string
  default = "preeyshah/cognition-demo"
}

variable "waf_rate_limit_per_5min" {
  type    = number
  default = 1000
}

variable "db_multi_az" {
  description = "Synchronous standby in a second AZ with automatic failover."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup / point-in-time-recovery window."
  type        = number
  default     = 14
}

variable "db_read_replica" {
  description = "Create a read replica (offload analytics, promotable for DR)."
  type        = bool
  default     = false
}

variable "dr_region" {
  description = "Region that receives a daily copy of every database backup."
  type        = string
  default     = "us-west-2"
}

variable "alert_email" {
  description = "Optional email subscribed to the alerts SNS topic."
  type        = string
  default     = ""
}
