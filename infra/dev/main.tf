# Minimal dev environment: 2 AZs of subnets (ALB + RDS subnet groups require it) but no NAT
# (tasks run in public subnets), one small Fargate task each, db.t4g.micro single-AZ, no
# WAF/autoscaling/AWS Backup, no deletion protection. Same architecture as prod.
#
# Init:
#   terraform init -backend-config="bucket=<state bucket>" -backend-config="region=us-east-1"
terraform {
  required_version = ">= 1.6"
  backend "s3" {
    key            = "refunds-console-dev/terraform.tfstate"
    dynamodb_table = "refunds-console-tflock"
    encrypt        = true
  }
}

variable "name" {
  type    = string
  default = "refunds-dev"
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

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = { app = var.name, managed_by = "terraform" }
  }
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags {
    tags = { app = var.name, managed_by = "terraform" }
  }
}

module "app" {
  source = "../modules/app"

  name        = var.name
  region      = var.region
  environment = "dev" # mock SSO + demo seed enabled, like docker compose

  # Any CloudFront distribution may serve the SPA; keeps api tasks independent of the
  # distribution resource so they deploy even while CloudFront creation is blocked.
  allowed_redirect_origins = "https://*.cloudfront.net,http://localhost:8080,http://localhost:5173"
  cors_origins             = "https://*.cloudfront.net,http://localhost:8080,http://localhost:5173"

  vpc_cidr                 = "10.50.0.0/16"
  az_count                 = 2 # ALB + RDS subnet groups require 2 AZs; everything else stays single-AZ
  enable_nat_gateway       = false
  tasks_in_public_subnets  = true
  enable_waf               = false
  enable_autoscaling       = false
  enable_backup            = false
  api_desired_count        = 1
  api_min_count            = 1
  api_max_count            = 1
  api_task_cpu             = 256
  api_task_memory          = 1024
  provider_task_cpu        = 256
  provider_task_memory     = 512
  db_multi_az              = false
  db_backup_retention_days = 1
  db_deletion_protection   = false
  db_skip_final_snapshot   = true

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
