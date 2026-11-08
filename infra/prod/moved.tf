# State address moves: resources were refactored into ../modules/app. These blocks tell
# Terraform to update existing objects in place instead of destroying and recreating them.
# Safe to delete once every environment has applied with the module layout.

moved {
  from = aws_ecr_repository.backend
  to   = module.app.aws_ecr_repository.backend
}

moved {
  from = aws_ecr_lifecycle_policy.backend
  to   = module.app.aws_ecr_lifecycle_policy.backend
}

moved {
  from = aws_ecs_cluster.main
  to   = module.app.aws_ecs_cluster.main
}

moved {
  from = aws_cloudwatch_log_group.app
  to   = module.app.aws_cloudwatch_log_group.app
}

moved {
  from = aws_service_discovery_private_dns_namespace.internal
  to   = module.app.aws_service_discovery_private_dns_namespace.internal
}

moved {
  from = aws_service_discovery_service.provider
  to   = module.app.aws_service_discovery_service.provider
}

moved {
  from = aws_iam_role.execution
  to   = module.app.aws_iam_role.execution
}

moved {
  from = aws_iam_role_policy_attachment.execution
  to   = module.app.aws_iam_role_policy_attachment.execution
}

moved {
  from = aws_iam_role_policy.execution_secrets
  to   = module.app.aws_iam_role_policy.execution_secrets
}

moved {
  from = aws_iam_role.task
  to   = module.app.aws_iam_role.task
}

moved {
  from = aws_ecs_task_definition.api
  to   = module.app.aws_ecs_task_definition.api
}

moved {
  from = aws_ecs_task_definition.provider
  to   = module.app.aws_ecs_task_definition.provider
}

moved {
  from = aws_ecs_service.api
  to   = module.app.aws_ecs_service.api
}

moved {
  from = aws_ecs_service.provider
  to   = module.app.aws_ecs_service.provider
}

moved {
  from = aws_kms_key.app
  to   = module.app.aws_kms_key.app
}

moved {
  from = aws_db_parameter_group.pg
  to   = module.app.aws_db_parameter_group.pg
}

moved {
  from = aws_db_instance.main
  to   = module.app.aws_db_instance.main
}

moved {
  from = random_password.jwt
  to   = module.app.random_password.jwt
}

moved {
  from = random_password.provider_key
  to   = module.app.random_password.provider_key
}

moved {
  from = random_password.payments_webhook
  to   = module.app.random_password.payments_webhook
}

moved {
  from = aws_secretsmanager_secret.app
  to   = module.app.aws_secretsmanager_secret.app
}

moved {
  from = aws_secretsmanager_secret_version.app
  to   = module.app.aws_secretsmanager_secret_version.app
}

moved {
  from = aws_secretsmanager_secret.devin
  to   = module.app.aws_secretsmanager_secret.devin
}

moved {
  from = aws_secretsmanager_secret_version.devin
  to   = module.app.aws_secretsmanager_secret_version.devin
}

moved {
  from = aws_security_group.alb
  to   = module.app.aws_security_group.alb
}

moved {
  from = aws_security_group.api
  to   = module.app.aws_security_group.api
}

moved {
  from = aws_security_group.provider
  to   = module.app.aws_security_group.provider
}

moved {
  from = aws_security_group.db
  to   = module.app.aws_security_group.db
}

moved {
  from = random_password.origin_verify
  to   = module.app.random_password.origin_verify
}

moved {
  from = aws_lb.api
  to   = module.app.aws_lb.api
}

moved {
  from = aws_lb_target_group.api
  to   = module.app.aws_lb_target_group.api
}

moved {
  from = aws_lb_listener.http
  to   = module.app.aws_lb_listener.http
}

moved {
  from = aws_lb_listener_rule.from_cloudfront
  to   = module.app.aws_lb_listener_rule.from_cloudfront
}

moved {
  from = aws_s3_bucket.frontend
  to   = module.app.aws_s3_bucket.frontend
}

moved {
  from = aws_s3_bucket_public_access_block.frontend
  to   = module.app.aws_s3_bucket_public_access_block.frontend
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.frontend
  to   = module.app.aws_s3_bucket_server_side_encryption_configuration.frontend
}

moved {
  from = aws_cloudfront_origin_access_control.frontend
  to   = module.app.aws_cloudfront_origin_access_control.frontend
}

moved {
  from = aws_s3_bucket_policy.frontend
  to   = module.app.aws_s3_bucket_policy.frontend
}

moved {
  from = aws_cloudfront_function.strip_api_prefix
  to   = module.app.aws_cloudfront_function.strip_api_prefix
}

moved {
  from = aws_cloudfront_function.spa_routes
  to   = module.app.aws_cloudfront_function.spa_routes
}

moved {
  from = aws_cloudfront_response_headers_policy.security
  to   = module.app.aws_cloudfront_response_headers_policy.security
}

moved {
  from = aws_cloudfront_distribution.main
  to   = module.app.aws_cloudfront_distribution.main
}

moved {
  from = aws_wafv2_web_acl.edge
  to   = module.app.aws_wafv2_web_acl.edge
}

moved {
  from = aws_sns_topic.alerts
  to   = module.app.aws_sns_topic.alerts
}

moved {
  from = aws_cloudwatch_metric_alarm.api_5xx
  to   = module.app.aws_cloudwatch_metric_alarm.api_5xx
}

moved {
  from = aws_cloudwatch_metric_alarm.unhealthy
  to   = module.app.aws_cloudwatch_metric_alarm.unhealthy
}

moved {
  from = aws_cloudwatch_query_definition.trace
  to   = module.app.aws_cloudwatch_query_definition.trace
}

moved {
  from = aws_appautoscaling_target.api
  to   = module.app.aws_appautoscaling_target.api
}

moved {
  from = aws_appautoscaling_policy.api_cpu
  to   = module.app.aws_appautoscaling_policy.api_cpu
}

moved {
  from = aws_appautoscaling_policy.api_memory
  to   = module.app.aws_appautoscaling_policy.api_memory
}

moved {
  from = aws_appautoscaling_policy.api_requests
  to   = module.app.aws_appautoscaling_policy.api_requests
}

moved {
  from = aws_iam_role.rds_monitoring
  to   = module.app.aws_iam_role.rds_monitoring
}

moved {
  from = aws_iam_role_policy_attachment.rds_monitoring
  to   = module.app.aws_iam_role_policy_attachment.rds_monitoring
}

moved {
  from = aws_db_instance.replica
  to   = module.app.aws_db_instance.replica
}

moved {
  from = aws_kms_key.dr
  to   = module.app.aws_kms_key.dr
}

moved {
  from = aws_backup_vault.primary
  to   = module.app.aws_backup_vault.primary
}

moved {
  from = aws_backup_vault.dr
  to   = module.app.aws_backup_vault.dr
}

moved {
  from = aws_backup_plan.db
  to   = module.app.aws_backup_plan.db
}

moved {
  from = aws_iam_role.backup
  to   = module.app.aws_iam_role.backup
}

moved {
  from = aws_iam_role_policy_attachment.backup
  to   = module.app.aws_iam_role_policy_attachment.backup
}

moved {
  from = aws_backup_selection.db
  to   = module.app.aws_backup_selection.db
}

moved {
  from = aws_cloudwatch_metric_alarm.db
  to   = module.app.aws_cloudwatch_metric_alarm.db
}

moved {
  from = aws_cloudwatch_metric_alarm.replica_lag
  to   = module.app.aws_cloudwatch_metric_alarm.replica_lag
}

moved {
  from = aws_db_event_subscription.db
  to   = module.app.aws_db_event_subscription.db
}

moved {
  from = aws_sns_topic_subscription.email
  to   = module.app.aws_sns_topic_subscription.email
}

moved {
  from = aws_cloudwatch_dashboard.db
  to   = module.app.aws_cloudwatch_dashboard.db
}

moved {
  from = module.vpc
  to   = module.app.module.vpc
}

