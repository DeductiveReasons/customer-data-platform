# ---------- Query layer: Athena tables over S3 ----------

locals {
  people_prefix  = "analytics/people/"
  history_prefix = "history/"
  bucket_url     = "s3://${aws_s3_bucket.raw.bucket}"
  athena_db      = replace("${var.name}_analytics", "-", "_")

  engagement_columns = {
    message_id    = "string"
    customer_id   = "string"
    touch         = "string"
    asset_id      = "string"
    channel       = "string"
    selection     = "string"
    model_version = "string"
    image_id      = "string"
    image_theme   = "string"
    opened        = "boolean"
    clicked       = "boolean"
    registered    = "boolean"
    unsubscribed  = "boolean"
    feedback_at   = "string"
  }

  athena_tables = {
    people = {
      description = "One row per person with their current next best action. Refreshed by the export function."
      location    = "${local.bucket_url}/${local.people_prefix}"
      columns = {
        customer_id              = "string"
        first_name               = "string"
        last_name                = "string"
        email                    = "string"
        address                  = "string"
        city                     = "string"
        postal_code              = "string"
        state                    = "string"
        age                      = "int"
        tenure_months            = "int"
        app_sessions_30d         = "int"
        days_since_active        = "int"
        email_open_rate          = "double"
        wellness_member          = "int"
        consent_marketing        = "boolean"
        consent_email            = "boolean"
        consent_direct_mail      = "boolean"
        touches                  = "int"
        opens                    = "int"
        clicks                   = "int"
        registered               = "boolean"
        onboarded                = "boolean"
        last_asset               = "string"
        status                   = "string"
        next_best_action         = "string"
        next_best_action_time    = "string"
        no_contact_reason        = "string"
        next_best_asset          = "string"
        next_best_channel        = "string"
        next_best_propensity     = "double"
        next_best_expected_value = "double"
        next_best_image          = "string"
        next_best_image_theme    = "string"
        model_version            = "string"
        snapshot_at              = "string"
      }
    }
    nba_history = {
      description = "Every next best action message that was sent."
      location    = "${local.bucket_url}/${local.history_prefix}"
      columns = {
        message_id     = "string"
        customer_id    = "string"
        touch          = "string"
        asset_id       = "string"
        channel        = "string"
        selection      = "string"
        propensity     = "double"
        expected_value = "double"
        model_version  = "string"
        image_id       = "string"
        image_theme    = "string"
        content_score  = "double"
        trigger        = "string"
        decided_at     = "string"
      }
    }
    email_engagement = {
      description = "Simulated email outcomes: opened, clicked, registered and unsubscribed."
      location    = "${local.bucket_url}/${local.feedback_prefix}email/"
      columns     = local.engagement_columns
    }
    direct_mail_engagement = {
      description = "Simulated direct mail outcomes. Mail reads cannot be observed, so opened is always false."
      location    = "${local.bucket_url}/${local.feedback_prefix}direct_mail/"
      columns     = local.engagement_columns
    }
  }

  saved_queries = {
    "1 Next best action for each person" = "SELECT customer_id, first_name, last_name, status, next_best_action, next_best_action_time, no_contact_reason, next_best_asset, next_best_propensity, next_best_expected_value FROM people ORDER BY next_best_expected_value DESC NULLS LAST LIMIT 100;"
    "2 People by status"                 = "SELECT status, next_best_action, COUNT(*) AS people FROM people GROUP BY status, next_best_action ORDER BY people DESC;"
    "3 One customer's history"           = "SELECT h.decided_at, h.touch, h.asset_id, h.channel, h.selection, h.propensity, COALESCE(e.opened, d.opened) AS opened, COALESCE(e.clicked, d.clicked) AS clicked, COALESCE(e.registered, d.registered) AS registered FROM nba_history h LEFT JOIN email_engagement e ON h.message_id = e.message_id LEFT JOIN direct_mail_engagement d ON h.message_id = d.message_id WHERE h.customer_id = (SELECT customer_id FROM people WHERE touches > 1 LIMIT 1) ORDER BY h.decided_at;"
    "4 Email funnel by asset"            = "SELECT asset_id, COUNT(DISTINCT message_id) AS delivered, COUNT(DISTINCT CASE WHEN opened THEN message_id END) AS opened, COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked, COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered FROM email_engagement GROUP BY asset_id ORDER BY registered DESC;"
    "5 Direct mail funnel by asset"      = "SELECT asset_id, COUNT(DISTINCT message_id) AS delivered, COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS qr_visits, COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered FROM direct_mail_engagement GROUP BY asset_id ORDER BY registered DESC;"
    "6 Sent by touch and channel"        = "SELECT touch, channel, COUNT(DISTINCT message_id) AS messages FROM nba_history GROUP BY touch, channel ORDER BY touch, channel;"
  }
}

resource "aws_glue_catalog_database" "analytics" {
  name = local.athena_db
}

resource "aws_glue_catalog_table" "analytics" {
  for_each      = local.athena_tables
  name          = each.key
  database_name = aws_glue_catalog_database.analytics.name
  description   = each.value.description
  table_type    = "EXTERNAL_TABLE"
  parameters    = { classification = "json" }

  storage_descriptor {
    location      = each.value.location
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters            = { "ignore.malformed.json" = "true" }
    }

    dynamic "columns" {
      for_each = each.value.columns
      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

resource "aws_athena_workgroup" "analytics" {
  name          = var.name
  force_destroy = true

  configuration {
    enforce_workgroup_configuration = true
    bytes_scanned_cutoff_per_query  = 1073741824

    result_configuration {
      output_location = "${local.bucket_url}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

resource "aws_athena_named_query" "saved" {
  for_each  = local.saved_queries
  name      = each.key
  workgroup = aws_athena_workgroup.analytics.id
  database  = aws_glue_catalog_database.analytics.name
  query     = each.value
}

# ---------- People table export ----------

resource "aws_iam_role" "export" {
  name = "${var.name}-nba-export"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "export" {
  role = aws_iam_role.export.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.logs_statement,
      {
        Effect   = "Allow"
        Action   = "dynamodb:Scan"
        Resource = aws_dynamodb_table.profiles.arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.raw.arn}/${local.people_prefix}*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.raw.arn}/${local.model_key}"
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = local.metric_namespace } }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "export" {
  name              = "/aws/lambda/${var.name}-nba-export"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "export" {
  function_name    = "${var.name}-nba-export"
  role             = aws_iam_role.export.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "cdp.nba_export.handler"
  timeout          = 900
  memory_size      = 1024
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      TABLE_NAME       = aws_dynamodb_table.profiles.name
      BUCKET           = aws_s3_bucket.raw.bucket
      PEOPLE_KEY       = "${local.people_prefix}latest.json.gz"
      MODEL_KEY        = local.model_key
      APPROVALS_TABLE  = aws_dynamodb_table.approvals.name
      METRIC_NAMESPACE = local.metric_namespace
    }
  }

  depends_on = [aws_cloudwatch_log_group.export, aws_iam_role_policy.export]
}

resource "aws_cloudwatch_event_rule" "export" {
  name                = "${var.name}-nba-export"
  schedule_expression = "rate(1 hour)"
  state               = var.nba_schedule_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "export" {
  rule = aws_cloudwatch_event_rule.export.name
  arn  = aws_lambda_function.export.arn
}

resource "aws_lambda_permission" "export" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.export.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.export.arn
}

# ---------- Dashboard ----------

locals {
  channels      = ["email", "direct_mail"]
  funnel_metric = ["Delivered", "Opened", "Clicked", "Registered", "Unsubscribed"]

  dashboard_widgets = concat(
    [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "## Eric Health sign-up demo\nAll people and engagement are simulated, and Eric Health is fictional. Totals update when the export function runs. Query the detail in Athena: workgroup `${var.name}`, database `${local.athena_db}`."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 24
        height = 4
        properties = {
          title   = "People (latest export)"
          view    = "singleValue"
          region  = local.region
          period  = 86400
          stat    = "Maximum"
          metrics = [for name in ["People", "PeopleContacted", "PeopleRegistered", "PeopleWithNextAction"] : [local.metric_namespace, name]]
        }
      },
    ],
    [
      for index, channel in local.channels : {
        type   = "metric"
        x      = index * 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "${channel} engagement"
          view    = "timeSeries"
          stacked = false
          region  = local.region
          period  = 300
          stat    = "Sum"
          metrics = [for name in local.funnel_metric : [local.metric_namespace, name, "Channel", channel]]
        }
      }
    ],
    [
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Queue backlog"
          view   = "timeSeries"
          region = local.region
          period = 60
          stat   = "Maximum"
          metrics = [
            for queue in [aws_sqs_queue.processing, aws_sqs_queue.nba_decisions, aws_sqs_queue.feedback] :
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue.name]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Failed messages (dead-letter queues)"
          view   = "timeSeries"
          region = local.region
          period = 60
          stat   = "Maximum"
          metrics = [
            for queue in values(local.dlqs) :
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", queue]
          ]
        }
      },
    ],
  )
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.name}-nba"
  dashboard_body = jsonencode({ widgets = local.dashboard_widgets })
}
