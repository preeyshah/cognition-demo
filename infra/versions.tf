terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Configured at init: terraform init -backend-config="bucket=<state bucket>" -backend-config="region=<region>"
  backend "s3" {
    key            = "refunds-console/terraform.tfstate"
    dynamodb_table = "refunds-console-tflock"
    encrypt        = true
  }
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
