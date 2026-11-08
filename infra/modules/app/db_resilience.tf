# Database monitoring + redundancy: Enhanced Monitoring, optional read replica, AWS Backup with a
# cross-region copy, CloudWatch alarms and RDS event notifications (all to the alerts SNS topic).

resource "aws_iam_role" "rds_monitoring" {
  name = "${var.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "monitoring.rds.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "replica" {
  count                        = var.db_read_replica ? 1 : 0
  identifier                   = "${var.name}-replica"
  replicate_source_db          = aws_db_instance.main.identifier
  instance_class               = var.db_instance_class
  storage_encrypted            = true
  kms_key_id                   = aws_kms_key.app.arn
  vpc_security_group_ids       = [aws_security_group.db.id]
  parameter_group_name         = aws_db_parameter_group.pg.name
  publicly_accessible          = false
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.rds_monitoring.arn
  performance_insights_enabled = true
  skip_final_snapshot          = true
  auto_minor_version_upgrade   = true
}

# --- AWS Backup: daily + weekly, copied to a vault in the DR region ---
resource "aws_kms_key" "dr" {
  count               = var.enable_backup ? 1 : 0
  provider            = aws.dr
  description         = "${var.name} DR backup vault"
  enable_key_rotation = true
}

resource "aws_backup_vault" "primary" {
  count       = var.enable_backup ? 1 : 0
  name        = "${var.name}-primary"
  kms_key_arn = aws_kms_key.app.arn
}

resource "aws_backup_vault" "dr" {
  count       = var.enable_backup ? 1 : 0
  provider    = aws.dr
  name        = "${var.name}-dr"
  kms_key_arn = aws_kms_key.dr[0].arn
}

resource "aws_backup_plan" "db" {
  count = var.enable_backup ? 1 : 0
  name  = "${var.name}-db"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.primary[0].name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle { delete_after = 35 }
    copy_action {
      destination_vault_arn = aws_backup_vault.dr[0].arn
      lifecycle { delete_after = 35 }
    }
  }

  rule {
    rule_name         = "weekly"
    target_vault_name = aws_backup_vault.primary[0].name
    schedule          = "cron(0 6 ? * SUN *)"
    lifecycle { delete_after = 180 }
    copy_action {
      destination_vault_arn = aws_backup_vault.dr[0].arn
      lifecycle { delete_after = 180 }
    }
  }
}

resource "aws_iam_role" "backup" {
  count = var.enable_backup ? 1 : 0
  name  = "${var.name}-backup"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "backup.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  for_each   = var.enable_backup ? toset(["AWSBackupServiceRolePolicyForBackup", "AWSBackupServiceRolePolicyForRestores"]) : toset([])
  role       = aws_iam_role.backup[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/${each.value}"
}

resource "aws_backup_selection" "db" {
  count        = var.enable_backup ? 1 : 0
  name         = "${var.name}-db"
  plan_id      = aws_backup_plan.db[0].id
  iam_role_arn = aws_iam_role.backup[0].arn
  resources    = [aws_db_instance.main.arn]
}

# --- Alarms ---
locals {
  db_alarms = {
    cpu         = { metric = "CPUUtilization", stat = "Average", op = "GreaterThanThreshold", threshold = 80, desc = "CPU > 80% for 15 min" }
    storage     = { metric = "FreeStorageSpace", stat = "Minimum", op = "LessThanThreshold", threshold = 2 * 1024 * 1024 * 1024, desc = "Free storage < 2 GiB" }
    memory      = { metric = "FreeableMemory", stat = "Minimum", op = "LessThanThreshold", threshold = 100 * 1024 * 1024, desc = "Freeable memory < 100 MiB" }
    connections = { metric = "DatabaseConnections", stat = "Maximum", op = "GreaterThanThreshold", threshold = 70, desc = "Connections > 70 (t4g.micro max ~ 80)" }
    read_lat    = { metric = "ReadLatency", stat = "Average", op = "GreaterThanThreshold", threshold = 0.05, desc = "Read latency > 50 ms" }
    write_lat   = { metric = "WriteLatency", stat = "Average", op = "GreaterThanThreshold", threshold = 0.05, desc = "Write latency > 50 ms" }
  }
}

resource "aws_cloudwatch_metric_alarm" "db" {
  for_each            = local.db_alarms
  alarm_name          = "${var.name}-db-${each.key}"
  alarm_description   = each.value.desc
  namespace           = "AWS/RDS"
  metric_name         = each.value.metric
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = 3
  threshold           = each.value.threshold
  comparison_operator = each.value.op
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "replica_lag" {
  count               = var.db_read_replica ? 1 : 0
  alarm_name          = "${var.name}-db-replica-lag"
  namespace           = "AWS/RDS"
  metric_name         = "ReplicaLag"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.replica[0].identifier }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  threshold           = 60
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_db_event_subscription" "db" {
  name             = "${var.name}-db-events"
  sns_topic        = aws_sns_topic.alerts.arn
  source_type      = "db-instance"
  source_ids       = concat([aws_db_instance.main.identifier], aws_db_instance.replica[*].identifier)
  event_categories = ["availability", "failover", "failure", "low storage", "maintenance", "recovery", "restoration"]
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_dashboard" "db" {
  dashboard_name = "${var.name}-db"
  dashboard_body = jsonencode({
    widgets = [for i, m in ["CPUUtilization", "DatabaseConnections", "FreeStorageSpace", "FreeableMemory", "ReadLatency", "WriteLatency"] : {
      type = "metric", x = (i % 3) * 8, y = floor(i / 3) * 6, width = 8, height = 6
      properties = {
        title   = m, region = var.region, stat = "Average", period = 300
        metrics = [["AWS/RDS", m, "DBInstanceIdentifier", aws_db_instance.main.identifier]]
      }
    }]
  })
}
