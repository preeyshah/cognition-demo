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
  description = "Devin org id for the self-healing loop (the API key itself lives in Secrets Manager)."
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
