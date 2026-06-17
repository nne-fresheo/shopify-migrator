terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = "eu-west-1"
  profile = "fresheo-dev"
}

locals {
  name        = "shopify-migrator"
  environment = "dev"
  tags = {
    Project     = "shopify-migrator"
    Environment = local.environment
    ManagedBy   = "Terraform"
  }
}

# ── References to existing infra (fresheo-dev VPC + Aurora) ──────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_vpc" "main" {
  id = "vpc-0e539beb150197dc6"
}

data "aws_subnet" "fargate_public" {
  id = "subnet-042aa8b99eeafd5c4"
}

data "aws_rds_cluster" "db" {
  cluster_identifier = "databaseclusteraurorapgsql"
}

# ── Security group for the Fargate task ──────────────────────────────────────

resource "aws_security_group" "fargate" {
  name_prefix = "${local.name}-"
  vpc_id      = data.aws_vpc.main.id
  description = "shopify-migrator Fargate task - outbound only"

  tags = merge(local.tags, { Name = local.name })
}

resource "aws_vpc_security_group_egress_rule" "fargate_all" {
  security_group_id = aws_security_group.fargate.id
  description       = "All outbound (Shopify API, ECR, Secrets Manager, Aurora, CloudWatch)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "aurora_from_fargate" {
  security_group_id            = tolist(data.aws_rds_cluster.db.vpc_security_group_ids)[0]
  referenced_security_group_id = aws_security_group.fargate.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL from shopify-migrator Fargate task"
}

# ── ECR ──────────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "this" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ── Secrets Manager (one secret per tenant) ──────────────────────────────────
# Each secret is created empty; the operator populates the JSON value via the
# AWS console with all six DEST_* + DJANGO_* keys. Task definitions reference
# individual keys via the `:KEY::` ARN suffix.

resource "aws_secretsmanager_secret" "tenant" {
  for_each = var.tenants

  name                    = "${local.name}/${local.environment}/${each.key}"
  description             = coalesce(each.value.description, "shopify-migrator secrets for ${each.key}")
  recovery_window_in_days = 7
  tags                    = merge(local.tags, { Tenant = each.key })
}

# ── CloudWatch Logs ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
  tags              = local.tags
}

# ── IAM ──────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets" {
  count = length(aws_secretsmanager_secret.tenant) > 0 ? 1 : 0

  name = "read-tenant-secrets"
  role = aws_iam_role.task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.tenant : s.arn]
    }]
  })
}

# Task role: app-level perms. The migrator makes no AWS API calls today, so
# this role has no policies attached — kept separate from the execution role
# for clean separation if app-side perms are added later.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
  tags               = local.tags
}

# ── ECS cluster + task definition ────────────────────────────────────────────

resource "aws_ecs_cluster" "this" {
  name = local.name
  tags = local.tags
}

locals {
  secret_keys = [
    "DEST_SHOP_DOMAIN",
    "DEST_CLIENT_ID",
    "DEST_CLIENT_SECRET",
    "DEST_ACCESS_TOKEN",
    "DJANGO_DATABASE_URL",
    "DJANGO_MEDIA_URL",
  ]
}

resource "aws_ecs_task_definition" "job" {
  for_each = var.jobs

  family                   = "${local.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name      = "migrator"
    image     = "${aws_ecr_repository.this.repository_url}:latest"
    essential = true
    command   = each.value.command

    environment = concat(
      [
        { name = "API_VERSION", value = "2026-04" },
        { name = "LOG_LEVEL", value = "INFO" },
      ],
      [for k, v in each.value.env : { name = k, value = v }],
    )

    secrets = [
      for k in local.secret_keys : {
        name      = k
        valueFrom = "${aws_secretsmanager_secret.tenant[each.value.tenant].arn}:${k}::"
      }
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.this.name
        awslogs-region        = data.aws_region.current.region
        awslogs-stream-prefix = each.key
      }
    }
  }])

  tags = merge(local.tags, {
    Tenant = each.value.tenant
    Job    = each.key
  })
}

# ── EventBridge Scheduler ────────────────────────────────────────────────────

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "scheduler" {
  count = length(aws_ecs_task_definition.job) > 0 ? 1 : 0

  name = "run-task"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = "ecs:RunTask"
        Resource = [
          for td in aws_ecs_task_definition.job :
          "arn:aws:ecs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:task-definition/${td.family}:*"
        ]
        Condition = {
          ArnLike = {
            "ecs:cluster" = aws_ecs_cluster.this.arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.task_execution.arn,
          aws_iam_role.task.arn,
        ]
        Condition = {
          StringEquals = {
            "iam:PassedToService" = "ecs-tasks.amazonaws.com"
          }
        }
      },
    ]
  })
}

resource "aws_scheduler_schedule" "job" {
  for_each = var.jobs

  name        = "${local.name}-${each.key}"
  description = "Scheduled '${join(" ", each.value.command)}' for tenant ${each.value.tenant}"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = each.value.timezone
  state                        = each.value.enabled ? "ENABLED" : "DISABLED"

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.job[each.key].arn
      launch_type         = "FARGATE"
      platform_version    = "LATEST"

      network_configuration {
        subnets          = [data.aws_subnet.fargate_public.id]
        security_groups  = [aws_security_group.fargate.id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 0
    }
  }
}

# ── Outputs ──────────────────────────────────────────────────────────────────

output "ecr_repository_url" {
  value       = aws_ecr_repository.this.repository_url
  description = "Tag and push the migrator image here"
}

output "tenant_secret_arns" {
  value       = { for k, s in aws_secretsmanager_secret.tenant : k => s.arn }
  description = "Per-tenant Secrets Manager ARNs. Populate each secret's JSON value via AWS console with the six DEST_* + DJANGO_* keys."
}

output "task_definition_families" {
  value       = { for k, td in aws_ecs_task_definition.job : k => td.family }
  description = "Per-job task definition family names. Use with `aws ecs run-task --task-definition <family>` for ad-hoc runs."
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "fargate_subnet_id" {
  value = data.aws_subnet.fargate_public.id
}

output "fargate_security_group_id" {
  value = aws_security_group.fargate.id
}
