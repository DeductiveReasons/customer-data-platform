# ---------- Web app: performance, assets, people and monitoring ----------

resource "random_password" "webapp" {
  length  = 24
  special = false
}

resource "aws_secretsmanager_secret" "webapp" {
  name                    = "${var.name}/webapp-password"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "webapp" {
  secret_id     = aws_secretsmanager_secret.webapp.id
  secret_string = random_password.webapp.result
}

locals {
  webapp_queues = merge(
    { for q in concat(
      [aws_sqs_queue.processing, aws_sqs_queue.nba_decisions, aws_sqs_queue.feedback, aws_sqs_queue.archive],
      values(aws_sqs_queue.destination),
      [aws_sqs_queue.processing_dlq, aws_sqs_queue.nba_decisions_dlq, aws_sqs_queue.feedback_dlq, aws_sqs_queue.archive_dlq],
      values(aws_sqs_queue.destination_dlq),
    ) : q.name => q.url },
  )
  webapp_read_prefixes = [local.people_prefix, local.history_prefix, local.feedback_prefix]
}

resource "aws_iam_role" "webapp" {
  name = "${var.name}-webapp"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "webapp" {
  role = aws_iam_role.webapp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.logs_statement,
      {
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"]
        Resource = aws_athena_workgroup.analytics.arn
      },
      {
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${local.region}:${local.account_id}:catalog",
          aws_glue_catalog_database.analytics.arn,
          "arn:aws:glue:${local.region}:${local.account_id}:table/${aws_glue_catalog_database.analytics.name}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.raw.arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = [for p in local.webapp_read_prefixes : "${aws_s3_bucket.raw.arn}/${p}*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        Resource = "${aws_s3_bucket.raw.arn}/athena-results/*"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:GetQueueAttributes"
        Resource = "arn:aws:sqs:${local.region}:${local.account_id}:${var.name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:DescribeAlarms", "cloudwatch:GetMetricData"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.webapp.arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "webapp" {
  name              = "/aws/lambda/${var.name}-webapp"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "webapp" {
  function_name    = "${var.name}-webapp"
  role             = aws_iam_role.webapp.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "cdp.webapp.handler"
  timeout          = 29
  memory_size      = 512
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      SECRET_ARN       = aws_secretsmanager_secret.webapp.arn
      ATHENA_WORKGROUP = aws_athena_workgroup.analytics.name
      ATHENA_DATABASE  = aws_glue_catalog_database.analytics.name
      RESOURCE_PREFIX  = var.name
      QUEUES           = jsonencode(local.webapp_queues)
      FUNCTIONS = jsonencode(concat(
        [for f in aws_lambda_function.fn : f.function_name],
        [aws_lambda_function.receiver.function_name, aws_lambda_function.export.function_name],
      ))
      DASHBOARD_URL = "https://${local.region}.console.aws.amazon.com/cloudwatch/home?region=${local.region}#dashboards/dashboard/${aws_cloudwatch_dashboard.main.dashboard_name}"
    }
  }

  depends_on = [aws_cloudwatch_log_group.webapp, aws_iam_role_policy.webapp]
}

resource "aws_apigatewayv2_api" "webapp" {
  name          = "${var.name}-webapp"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "webapp" {
  api_id                 = aws_apigatewayv2_api.webapp.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.webapp.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webapp" {
  api_id    = aws_apigatewayv2_api.webapp.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.webapp.id}"
}

resource "aws_apigatewayv2_stage" "webapp" {
  api_id      = aws_apigatewayv2_api.webapp.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = 20
    throttling_burst_limit = 40
  }
}

resource "aws_lambda_permission" "webapp" {
  statement_id  = "AllowHttpApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.webapp.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webapp.execution_arn}/*/*"
}

output "webapp_url" {
  value = aws_apigatewayv2_stage.webapp.invoke_url
}

output "webapp_secret" {
  value = aws_secretsmanager_secret.webapp.name
}
