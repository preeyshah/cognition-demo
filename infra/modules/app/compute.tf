resource "aws_ecr_repository" "backend" {
  name                 = "${var.name}/backend"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.app.arn
  }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "keep last 30 images",
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 30 },
    action       = { type = "expire" }
  }] })
}

resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.app.arn
}

resource "aws_service_discovery_private_dns_namespace" "internal" {
  name = "${var.name}.internal"
  vpc  = module.vpc.vpc_id
}

resource "aws_service_discovery_service" "provider" {
  name = "provider"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: pull images, write logs, read exactly the secrets the task needs.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
    Resource = [aws_secretsmanager_secret.app.arn, aws_secretsmanager_secret.devin.arn, aws_db_instance.main.master_user_secret[0].secret_arn] },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [aws_kms_key.app.arn] },
  ] })
}

# Task role: the app itself needs no AWS API access.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

locals {
  image            = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  app_origin       = "https://${aws_cloudfront_distribution.main.domain_name}"
  tasks_subnets    = var.tasks_in_public_subnets ? module.vpc.public_subnets : module.vpc.private_subnets
  assign_public_ip = var.tasks_in_public_subnets
  log_config = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.app.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "app"
    }
  }
  container_security = {
    readonlyRootFilesystem = true
    user                   = "10001"
    linuxParameters        = { initProcessEnabled = true }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_task_cpu
  memory                   = var.api_task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(local.container_security, {
    name         = "api"
    image        = local.image
    essential    = true
    portMappings = [{ containerPort = 8000 }]
    environment = [
      { name = "DB_HOST", value = aws_db_instance.main.address },
      { name = "DB_NAME", value = "refunds" },
      { name = "HISTORY_PROVIDER_URL", value = "http://provider.${var.name}.internal:9000" },
      { name = "PAYMENTS_PROVIDER_URL", value = "http://provider.${var.name}.internal:9000" },
      { name = "PAYMENTS_SANDBOX_CONTROLS", value = "true" }, # mock provider only; set false with a real PSP
      { name = "ALLOWED_REDIRECT_ORIGINS", value = local.app_origin },
      { name = "CORS_ORIGINS", value = local.app_origin },
      { name = "TRUSTED_PROXY_HOPS", value = "2" }, # CloudFront -> ALB
      { name = "ENVIRONMENT", value = var.environment },
      { name = "DEVIN_REPO", value = var.github_repo },
      { name = "DEVIN_MAX_ACU", value = tostring(var.devin_max_acu) },
    ]
    secrets = [
      { name = "DB_USER", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::" },
      { name = "DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
      { name = "JWT_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:JWT_SECRET::" },
      { name = "HISTORY_PROVIDER_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:PROVIDER_API_KEY::" },
      { name = "PAYMENTS_PROVIDER_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:PROVIDER_API_KEY::" },
      { name = "PAYMENTS_WEBHOOK_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:PAYMENTS_WEBHOOK_SECRET::" },
      { name = "DEVIN_API_KEY", valueFrom = "${aws_secretsmanager_secret.devin.arn}:DEVIN_API_KEY::" },
      { name = "DEVIN_ORG_ID", valueFrom = "${aws_secretsmanager_secret.devin.arn}:DEVIN_ORG_ID::" },
    ]
    logConfiguration = local.log_config
  })])
}

resource "aws_ecs_task_definition" "provider" {
  family                   = "${var.name}-provider"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.provider_task_cpu
  memory                   = var.provider_task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([merge(local.container_security, {
    name         = "provider"
    image        = local.image
    essential    = true
    command      = ["uvicorn", "mock_provider.main:app", "--host", "0.0.0.0", "--port", "9000"]
    portMappings = [{ containerPort = 9000 }]
    # Webhooks go out through CloudFront like any external PSP's would (provider SG allows 443 egress).
    environment = [{ name = "PAYMENTS_WEBHOOK_URL", value = "${local.app_origin}/api/webhooks/paycore" }]
    secrets = [
      { name = "PROVIDER_API_KEY", valueFrom = "${aws_secretsmanager_secret.app.arn}:PROVIDER_API_KEY::" },
      { name = "PAYMENTS_WEBHOOK_SECRET", valueFrom = "${aws_secretsmanager_secret.app.arn}:PAYMENTS_WEBHOOK_SECRET::" },
    ]
    logConfiguration = local.log_config
  })])
}

resource "aws_ecs_service" "api" {
  name                   = "api"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = var.api_desired_count
  launch_type            = "FARGATE"
  enable_execute_command = false

  # Application Auto Scaling owns the running count after creation (see autoscaling.tf).
  lifecycle {
    ignore_changes = [desired_count]
  }
  network_configuration {
    subnets          = local.tasks_subnets
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = local.assign_public_ip
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.http, aws_secretsmanager_secret_version.devin, aws_secretsmanager_secret_version.app]
}

resource "aws_ecs_service" "provider" {
  name            = "provider"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.provider.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = local.tasks_subnets
    security_groups  = [aws_security_group.provider.id]
    assign_public_ip = local.assign_public_ip
  }
  service_registries {
    registry_arn = aws_service_discovery_service.provider.arn
  }
}
