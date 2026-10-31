data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/lambda.zip"
  excludes    = ["cdp/__pycache__"]
}

locals {
  logs_statement = {
    Effect   = "Allow"
    Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    Resource = "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name}-*:*"
  }

  core_lambdas = {
    ingest = {
      handler = "cdp.ingest.handler"
      timeout = 10
      environment = {
        EVENT_BUS_NAME = aws_cloudwatch_event_bus.main.name
      }
      policy = jsonencode([{
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = aws_cloudwatch_event_bus.main.arn
      }])
    }
    starter = {
      handler = "cdp.starter.handler"
      timeout = 30
      environment = {
        STATE_MACHINE_ARN = local.state_machine_arn
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = "states:StartExecution"
          Resource = local.state_machine_arn
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = aws_sqs_queue.processing.arn
        },
      ])
    }
    identity = {
      handler = "cdp.identity.handler"
      timeout = 15
      environment = {
        TABLE_NAME = aws_dynamodb_table.profiles.name
      }
      policy = jsonencode([{
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.profiles.arn
      }])
    }
    rules = {
      handler = "cdp.rules.handler"
      timeout = 10
      environment = {
        ENABLED_DESTINATIONS = jsonencode(var.enabled_destinations)
        DESTINATION_QUEUES   = jsonencode({ for k, q in aws_sqs_queue.destination : k => q.url })
      }
      policy = jsonencode([])
    }
  }

  delivery_lambdas = {
    for d in local.destinations : "deliver-${replace(d, "_", "-")}" => {
      handler = "cdp.deliver.handler"
      timeout = 20
      environment = {
        SECRET_ARN = aws_secretsmanager_secret.destination[d].arn
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = "secretsmanager:GetSecretValue"
          Resource = aws_secretsmanager_secret.destination[d].arn
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = aws_sqs_queue.destination[d].arn
        },
      ])
    }
  }

  lambdas = merge(local.core_lambdas, local.delivery_lambdas)
}

resource "aws_iam_role" "lambda" {
  for_each = local.lambdas
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.lambdas
  role     = aws_iam_role.lambda[each.key].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat([local.logs_statement], jsondecode(each.value.policy))
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = local.lambdas
  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "fn" {
  for_each         = local.lambdas
  function_name    = "${var.name}-${each.key}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = each.value.handler
  timeout          = each.value.timeout
  memory_size      = 256
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = each.value.environment
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]
}

resource "aws_lambda_event_source_mapping" "starter" {
  event_source_arn        = aws_sqs_queue.processing.arn
  function_name           = aws_lambda_function.fn["starter"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]
}

resource "aws_lambda_event_source_mapping" "delivery" {
  for_each                = local.destinations
  event_source_arn        = aws_sqs_queue.destination[each.key].arn
  function_name           = aws_lambda_function.fn["deliver-${replace(each.key, "_", "-")}"].arn
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
}

# ---------- Step Functions pipeline ----------

resource "aws_iam_role" "pipeline" {
  name = "${var.name}-pipeline"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "pipeline" {
  role = aws_iam_role.pipeline.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = [aws_lambda_function.fn["identity"].arn, aws_lambda_function.fn["rules"].arn]
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = [for q in aws_sqs_queue.destination : q.arn]
      },
    ]
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.name}-pipeline"
  role_arn = aws_iam_role.pipeline.arn

  definition = jsonencode({
    Comment = "Identity resolution, consent and rules, then fan out to destination queues"
    StartAt = "ResolveIdentity"
    States = {
      ResolveIdentity = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["identity"].arn
        Retry    = local.task_retry
        Next     = "EvaluateRules"
      }
      EvaluateRules = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["rules"].arn
        Retry    = local.task_retry
        Next     = "Enqueue"
      }
      Enqueue = {
        Type      = "Map"
        ItemsPath = "$.deliveries"
        ItemProcessor = {
          StartAt = "SendToDestination"
          States = {
            SendToDestination = {
              Type     = "Task"
              Resource = "arn:aws:states:::sqs:sendMessage"
              Parameters = {
                "QueueUrl.$"    = "$.queue_url"
                "MessageBody.$" = "$"
              }
              Retry = local.task_retry
              End   = true
            }
          }
        }
        End = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.pipeline]
}

locals {
  task_retry = [{
    ErrorEquals     = ["States.ALL"]
    IntervalSeconds = 2
    MaxAttempts     = 4
    BackoffRate     = 2
  }]
}
