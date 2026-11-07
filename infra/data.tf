resource "aws_kms_key" "app" {
  description         = "${var.name} data + secrets"
  enable_key_rotation = true
}

resource "aws_db_parameter_group" "pg" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
}

resource "aws_db_instance" "main" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
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

  backup_retention_period         = 7
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-final"
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.app.arn
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "provider_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name       = "${var.name}/app"
  kms_key_id = aws_kms_key.app.arn
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    JWT_SECRET       = random_password.jwt.result
    PROVIDER_API_KEY = random_password.provider_key.result
  })
}

# Filled in out-of-band (aws secretsmanager put-secret-value) so the key never touches state or the repo.
resource "aws_secretsmanager_secret" "devin" {
  name       = "${var.name}/devin-api-key"
  kms_key_id = aws_kms_key.app.arn
}
