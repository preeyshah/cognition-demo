# One-time, run with admin credentials: state bucket + lock table + GitHub OIDC deploy role.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "github_repo" {
  type    = string
  default = "preeyshah/cognition-demo"
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "me" {}

resource "aws_s3_bucket" "state" {
  bucket = "refunds-console-tfstate-${data.aws_caller_identity.me.account_id}"
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
  }
}

resource "aws_dynamodb_table" "lock" {
  name         = "refunds-console-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# Only workflows running on main of this repo (environment "production") can assume the role.
resource "aws_iam_role" "deploy" {
  name = "refunds-console-github-deploy"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow",
    Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
    Action    = "sts:AssumeRoleWithWebIdentity",
    Condition = {
      StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:environment:production"
      }
    }
  }] })
}

# Terraform manages VPC/ECS/RDS/WAF/IAM roles, so the deploy role needs broad rights in this (dedicated) account.
resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Read-only role for `terraform plan` on pull requests (infra-plan workflow). Assumable only by
# pull_request runs of this repo — never by pushes or forks from other repos.
resource "aws_iam_role" "plan" {
  name = "refunds-console-github-plan"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow",
    Principal = { Federated = aws_iam_openid_connect_provider.github.arn },
    Action    = "sts:AssumeRoleWithWebIdentity",
    Condition = {
      StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:pull_request"
      }
    }
  }] })
}

resource "aws_iam_role_policy_attachment" "plan" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess lacks a few calls terraform needs: secret values (state refresh), KMS decrypt for
# those secrets, and the DynamoDB state lock.
resource "aws_iam_role_policy" "plan_extra" {
  role = aws_iam_role.plan.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = "*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:DescribeKey"],
    Resource = "arn:aws:kms:*:${data.aws_caller_identity.me.account_id}:key/*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
    Resource = aws_dynamodb_table.lock.arn },
  ] })
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "plan_role_arn" {
  value = aws_iam_role.plan.arn
}
