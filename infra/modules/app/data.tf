resource "aws_kms_key" "app" {
  description         = "${var.name} data + secrets"
  enable_key_rotation = true
}

locals {
  # Default Postgres settings; var.db_parameters can override or extend these via PR.
  db_pg_parameters = merge({
    "rds.force_ssl"              = "1"
    "log_min_duration_statement" = "500"
  }, var.db_parameters)
}

resource "aws_db_parameter_group" "pg" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  dynamic "parameter" {
    for_each = local.db_pg_parameters
    content {
      name  = parameter.key
      value = parameter.value
    }
  }
}

resource "aws_db_instance" "main" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.app.arn

  db_name                       = "refunds"
  username                      = "refunds_app"
  manage_master_user_password   = true # password generated + rotated in Secrets Manager
  master_user_secret_kms_key_id = aws_kms_key.app.key_id

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.pg.name
  publicly_accessible    = false

  # Redundancy: synchronous standby in a second AZ (automatic failover), PITR backups, Enhanced Monitoring.
  multi_az                              = var.db_multi_az
  backup_retention_period               = var.db_backup_retention_days
  backup_window                         = "03:00-04:00"
  maintenance_window                    = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot                 = true
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  performance_insights_retention_period = 7
  deletion_protection                   = var.db_deletion_protection
  skip_final_snapshot                   = var.db_skip_final_snapshot
  final_snapshot_identifier             = var.db_skip_final_snapshot ? null : "${var.name}-final"
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.app.arn
  enabled_cloudwatch_logs_exports       = ["postgresql"]
  auto_minor_version_upgrade            = true
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "provider_key" {
  length  = 40
  special = false
}

resource "random_password" "payments_webhook" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name       = "${var.name}/app"
  kms_key_id = aws_kms_key.app.arn
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    JWT_SECRET              = random_password.jwt.result
    PROVIDER_API_KEY        = random_password.provider_key.result
    PAYMENTS_WEBHOOK_SECRET = random_password.payments_webhook.result
  })
}

# Starts empty (self-healing off) so the first deploy works; the real key is set out-of-band with
# `aws secretsmanager put-secret-value` and Terraform ignores it afterwards, so it never touches the repo.
resource "aws_secretsmanager_secret" "devin" {
  name       = "${var.name}/devin-api-key"
  kms_key_id = aws_kms_key.app.arn
}

resource "aws_secretsmanager_secret_version" "devin" {
  secret_id     = aws_secretsmanager_secret.devin.id
  secret_string = jsonencode({ DEVIN_API_KEY = "", DEVIN_ORG_ID = var.devin_org_id })
  lifecycle {
    ignore_changes = [secret_string]
  }
}
