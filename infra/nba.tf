# ---------- Next best action pipeline ----------

locals {
  nba_lambdas = {
    nba-scan = {
      handler = "cdp.nba_scan.handler"
      memory  = 1024
      timeout = 900
      environment = {
        TABLE_NAME         = aws_dynamodb_table.profiles.name
        DECISION_QUEUE_URL = aws_sqs_queue.nba_decisions.url
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = "dynamodb:Scan"
          Resource = aws_dynamodb_table.profiles.arn
        },
        {
          Effect   = "Allow"
          Action   = "sqs:SendMessage"
          Resource = aws_sqs_queue.nba_decisions.arn
        },
      ])
    }
    nba-decide = {
      handler = "cdp.nba_decide.handler"
      memory  = 512
      timeout = 60
      environment = {
        TABLE_NAME          = aws_dynamodb_table.profiles.name
        RECEIVER_URL        = aws_apigatewayv2_stage.receiver.invoke_url
        RECEIVER_SECRET_ARN = aws_secretsmanager_secret.receiver.arn
        MODEL_BUCKET        = aws_s3_bucket.raw.bucket
        MODEL_KEY           = local.model_key
        ARCHIVE_QUEUE_URL   = aws_sqs_queue.archive.url
        APPROVALS_TABLE     = aws_dynamodb_table.approvals.name
        CONTENT_BASE_URL    = aws_apigatewayv2_api.webapp.api_endpoint
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"]
          Resource = aws_dynamodb_table.profiles.arn
        },
        {
          Effect   = "Allow"
          Action   = "sqs:SendMessage"
          Resource = aws_sqs_queue.archive.arn
        },
        {
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = "${aws_s3_bucket.raw.arn}/${local.model_key}"
        },
        {
          Effect   = "Allow"
          Action   = "secretsmanager:GetSecretValue"
          Resource = aws_secretsmanager_secret.receiver.arn
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = aws_sqs_queue.nba_decisions.arn
        },
      ])
    }
    nba-feedback = {
      handler = "cdp.nba_feedback.handler"
      timeout = 60
      environment = {
        TABLE_NAME         = aws_dynamodb_table.profiles.name
        ARCHIVE_QUEUE_URL  = aws_sqs_queue.archive.url
        DECISION_QUEUE_URL = aws_sqs_queue.nba_decisions.url
        METRIC_NAMESPACE   = local.metric_namespace
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
          Resource = aws_dynamodb_table.profiles.arn
        },
        {
          Effect   = "Allow"
          Action   = "sqs:SendMessage"
          Resource = aws_sqs_queue.archive.arn
        },
        {
          Effect   = "Allow"
          Action   = "sqs:SendMessage"
          Resource = aws_sqs_queue.nba_decisions.arn
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = aws_sqs_queue.feedback.arn
        },
      ])
    }
    archive = {
      handler = "cdp.archive.handler"
      timeout = 60
      environment = {
        BUCKET = aws_s3_bucket.raw.bucket
        DATASET_PREFIXES = jsonencode({
          raw                  = "events/"
          history              = local.history_prefix
          feedback_email       = "${local.feedback_prefix}email/"
          feedback_direct_mail = "${local.feedback_prefix}direct_mail/"
        })
      }
      policy = jsonencode([
        {
          Effect = "Allow"
          Action = "s3:PutObject"
          Resource = [
            "${aws_s3_bucket.raw.arn}/events/*",
            "${aws_s3_bucket.raw.arn}/${local.history_prefix}*",
            "${aws_s3_bucket.raw.arn}/${local.feedback_prefix}*",
          ]
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = aws_sqs_queue.archive.arn
        },
      ])
    }
    nba-retrain = {
      handler = "cdp.nba_retrain.handler"
      timeout = 900
      memory  = 3008
      environment = {
        MODEL_BUCKET    = aws_s3_bucket.raw.bucket
        MODEL_KEY       = local.model_key
        FEEDBACK_PREFIX = local.feedback_prefix
      }
      policy = jsonencode([
        {
          Effect   = "Allow"
          Action   = "s3:ListBucket"
          Resource = aws_s3_bucket.raw.arn
        },
        {
          Effect   = "Allow"
          Action   = "s3:GetObject"
          Resource = ["${aws_s3_bucket.raw.arn}/${local.feedback_prefix}*", "${aws_s3_bucket.raw.arn}/${local.model_key}"]
        },
        {
          Effect   = "Allow"
          Action   = "s3:PutObject"
          Resource = "${aws_s3_bucket.raw.arn}/${local.model_key}"
        },
      ])
    }
  }

  model_key        = "models/nba/latest.json"
  feedback_prefix  = "feedback/"
  metric_namespace = "${var.name}/NBA"
}

# ---------- Feedback loop ----------

resource "aws_sqs_queue" "feedback_dlq" {
  name                      = "${var.name}-feedback-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "feedback" {
  name                       = "${var.name}-feedback"
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.feedback_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_lambda_event_source_mapping" "nba_feedback" {
  event_source_arn        = aws_sqs_queue.feedback.arn
  function_name           = aws_lambda_function.fn["nba-feedback"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 40
  }
}

resource "aws_cloudwatch_event_rule" "nba_retrain" {
  name                = "${var.name}-nba-retrain"
  schedule_expression = var.nba_retrain_schedule
  state               = var.nba_schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "nba_retrain" {
  rule = aws_cloudwatch_event_rule.nba_retrain.name
  arn  = aws_lambda_function.fn["nba-retrain"].arn
}

resource "aws_lambda_permission" "nba_retrain" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["nba-retrain"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nba_retrain.arn
}

# ---------- Decision queue and schedule ----------

resource "aws_sqs_queue" "nba_decisions_dlq" {
  name                      = "${var.name}-nba-decisions-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "nba_decisions" {
  name                       = "${var.name}-nba-decisions"
  visibility_timeout_seconds = 360
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.nba_decisions_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_lambda_event_source_mapping" "nba_decide" {
  event_source_arn        = aws_sqs_queue.nba_decisions.arn
  function_name           = aws_lambda_function.fn["nba-decide"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 40
  }
}

# Disabled by default, so nothing runs on a schedule until it is switched on.
resource "aws_cloudwatch_event_rule" "nba_schedule" {
  name                = "${var.name}-nba-schedule"
  schedule_expression = var.nba_schedule
  state               = var.nba_schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "nba_schedule" {
  rule = aws_cloudwatch_event_rule.nba_schedule.name
  arn  = aws_lambda_function.fn["nba-scan"].arn
}

resource "aws_lambda_permission" "nba_schedule" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["nba-scan"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.nba_schedule.arn
}

# ---------- Dummy email and direct mail receiver ----------

resource "random_password" "receiver_token" {
  length  = 40
  special = false
}

# This token protects a dummy service. It is stored in Terraform state, which is not suitable for real credentials.
resource "aws_secretsmanager_secret" "receiver" {
  name                    = "${var.name}/receiver-token"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "receiver" {
  secret_id     = aws_secretsmanager_secret.receiver.id
  secret_string = random_password.receiver_token.result
}

resource "aws_dynamodb_table" "receiver" {
  name         = "${var.name}-receiver-messages"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "message_id"

  attribute {
    name = "message_id"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  server_side_encryption {
    enabled = true
  }
}

resource "aws_iam_role" "receiver" {
  name = "${var.name}-receiver"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "receiver" {
  role = aws_iam_role.receiver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.logs_statement,
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Scan"]
        Resource = aws_dynamodb_table.receiver.arn
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.receiver.arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.feedback.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "receiver" {
  name              = "/aws/lambda/${var.name}-receiver"
  retention_in_days = var.log_retention_days
}

# Kept outside local.lambdas because nba-decide needs this service's URL.
resource "aws_lambda_function" "receiver" {
  function_name    = "${var.name}-receiver"
  role             = aws_iam_role.receiver.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "cdp.receiver.handler"
  timeout          = 10
  memory_size      = 256
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      TABLE_NAME         = aws_dynamodb_table.receiver.name
      SECRET_ARN         = aws_secretsmanager_secret.receiver.arn
      FEEDBACK_QUEUE_URL = aws_sqs_queue.feedback.url
    }
  }

  depends_on = [aws_cloudwatch_log_group.receiver, aws_iam_role_policy.receiver]
}

resource "aws_apigatewayv2_api" "receiver" {
  name          = "${var.name}-receiver"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "receiver" {
  api_id                 = aws_apigatewayv2_api.receiver.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.receiver.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "receiver" {
  for_each  = toset(["POST /email", "POST /direct-mail", "GET /messages"])
  api_id    = aws_apigatewayv2_api.receiver.id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.receiver.id}"
}

resource "aws_apigatewayv2_stage" "receiver" {
  api_id      = aws_apigatewayv2_api.receiver.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "receiver" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.receiver.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.receiver.execution_arn}/*/*"
}

resource "aws_cloudwatch_metric_alarm" "receiver_errors" {
  alarm_name          = "${var.name}-errors-receiver"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.receiver.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
