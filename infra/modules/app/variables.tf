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
  description = "RDS instance class (e.g. db.t4g.micro, db.t4g.small, db.r6g.large). Change via PR to scale up/down."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "Initial RDS storage in GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "RDS storage autoscaling ceiling in GiB."
  type        = number
  default     = 100
}

variable "db_parameters" {
  description = "Extra Postgres parameter-group settings (name -> value), merged over the defaults in data.tf."
  type        = map(string)
  default     = {}
}

variable "api_desired_count" {
  description = "Initial api task count; autoscaling owns it afterwards (min capacity)."
  type        = number
  default     = 2
}

variable "api_min_count" {
  description = "Minimum api tasks (Application Auto Scaling floor)."
  type        = number
  default     = 2
}

variable "api_max_count" {
  description = "Maximum api tasks (Application Auto Scaling ceiling)."
  type        = number
  default     = 8
}

variable "api_task_cpu" {
  type    = number
  default = 512
}

variable "api_task_memory" {
  type    = number
  default = 1024
}

variable "provider_task_cpu" {
  type    = number
  default = 256
}

variable "provider_task_memory" {
  type    = number
  default = 512
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

# --- Environment sizing (dev sets these small; prod keeps the defaults) --------

variable "allowed_redirect_origins" {
  description = "Comma-separated origins the SSO flow may redirect back to. Supports '*' wildcards (e.g. https://*.cloudfront.net). Set per env so api tasks don't depend on the CloudFront resource existing."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Comma-separated CORS origins for the API. Supports '*' wildcards."
  type        = string
  default     = ""
}

variable "allow_public_alb" {
  description = "Dev escape hatch: allow the ALB to be reached directly (0.0.0.0/0, no origin-verify) while CloudFront is unavailable. Never enable on prod."
  type        = bool
  default     = false
}

variable "environment" {
  description = "Runtime mode passed to the API as ENVIRONMENT: 'prod' disables mock SSO + demo seed."
  type        = string
  default     = "prod"
}

variable "az_count" {
  description = "Availability zones to spread subnets/NAT across (1 for the cheapest dev env)."
  type        = number
  default     = 2
}

variable "enable_nat_gateway" {
  description = "NAT gateway for private subnets. Disable (with tasks_in_public_subnets) to save ~$32/mo on dev."
  type        = bool
  default     = true
}

variable "tasks_in_public_subnets" {
  description = "Run ECS tasks in public subnets with public IPs (required when enable_nat_gateway = false)."
  type        = bool
  default     = false
}

variable "enable_waf" {
  description = "Attach the CloudFront WAF web ACL (managed rules + rate limits)."
  type        = bool
  default     = true
}

variable "enable_autoscaling" {
  description = "Application Auto Scaling policies for the api service."
  type        = bool
  default     = true
}

variable "enable_backup" {
  description = "AWS Backup plans/vaults incl. the cross-region copy (and the aws.dr provider usage)."
  type        = bool
  default     = true
}

variable "db_deletion_protection" {
  description = "RDS deletion protection + final snapshot on destroy."
  type        = bool
  default     = true
}

variable "db_skip_final_snapshot" {
  description = "Skip the final snapshot when the instance is destroyed (dev only)."
  type        = bool
  default     = false
}
