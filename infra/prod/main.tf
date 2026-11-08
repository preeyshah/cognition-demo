# Production environment. All resources live in ../modules/app; moved.tf maps the
# pre-module state addresses so `terraform plan` shows updates in place, not replacements.
#
# Init (state bucket + lock table come from infra/bootstrap):
#   terraform init -backend-config="bucket=<state bucket>" -backend-config="region=us-east-1"
terraform {
  required_version = ">= 1.6"
  backend "s3" {
    key            = "refunds-console/terraform.tfstate"
    dynamodb_table = "refunds-console-tflock"
    encrypt        = true
  }
}

variable "name" {
  type    = string
  default = "refunds-console"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "dr_region" {
  type    = string
  default = "us-west-2"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { app = var.name, managed_by = "terraform" }
  }
}

# CloudFront-scoped WAF ACLs must live in us-east-1.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = { app = var.name, managed_by = "terraform" }
  }
}

# Disaster-recovery region for cross-region backup copies.
provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags {
    tags = { app = var.name, managed_by = "terraform" }
  }
}

module "app" {
  source = "../modules/app"

  name   = var.name
  region = var.region

  # Same wildcard as dev so api tasks don't depend on the distribution existing; tighten to
  # the exact origin (or a custom domain) once CloudFront is verified and stable.
  allowed_redirect_origins = "https://*.cloudfront.net"
  cors_origins             = "https://*.cloudfront.net"

  # Free-tier account limits: single-AZ only and short backup retention. Re-enable
  # multi_az / longer retention after upgrading the account plan.
  db_multi_az              = false
  db_backup_retention_days = 1

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
    aws.dr        = aws.dr
  }
}

output "app_url" {
  value = module.app.app_url
}

output "ecr_repository_url" {
  value = module.app.ecr_repository_url
}

output "frontend_bucket" {
  value = module.app.frontend_bucket
}

output "cloudfront_distribution_id" {
  value = module.app.cloudfront_distribution_id
}

output "ecs_cluster" {
  value = module.app.ecs_cluster
}

output "db_endpoint" {
  value = module.app.db_endpoint
}

output "db_replica_endpoint" {
  value = module.app.db_replica_endpoint
}

output "db_dashboard" {
  value = module.app.db_dashboard
}
