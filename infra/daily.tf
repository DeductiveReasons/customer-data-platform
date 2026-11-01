# ---------- Daily run: update the bandit, save results and forecast, then start the next day ----------

variable "daily_run_enabled" {
  description = "Run one campaign day each real day"
  type        = bool
  default     = true
}

variable "daily_new_leads" {
  description = "Average number of simulated new leads per day"
  type        = number
  default     = 200
}

resource "aws_iam_role" "daily" {
  name = "${var.name}-daily"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "daily" {
  role = aws_iam_role.daily.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "lambda:InvokeFunction"
      Resource = [
        aws_lambda_function.fn["nba-retrain"].arn,
        aws_lambda_function.export.arn,
        aws_lambda_function.fn["daily-advance"].arn,
      ]
    }]
  })
}

resource "aws_sfn_state_machine" "daily" {
  name     = "${var.name}-daily"
  role_arn = aws_iam_role.daily.arn

  definition = jsonencode({
    Comment = "Close the current day, then start the next one"
    StartAt = "UpdateBandit"
    States = {
      UpdateBandit = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["nba-retrain"].arn
        Retry    = local.task_retry
        Next     = "SaveResultsAndForecast"
      }
      SaveResultsAndForecast = {
        Type     = "Task"
        Resource = aws_lambda_function.export.arn
        Retry    = local.task_retry
        Next     = "StartNextDay"
      }
      StartNextDay = {
        Type     = "Task"
        Resource = aws_lambda_function.fn["daily-advance"].arn
        End      = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.daily]
}

resource "aws_iam_role" "daily_schedule" {
  name = "${var.name}-daily-schedule"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "daily_schedule" {
  role = aws_iam_role.daily_schedule.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.daily.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.name}-daily"
  schedule_expression = "rate(1 day)"
  state               = var.daily_run_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "daily" {
  rule     = aws_cloudwatch_event_rule.daily.name
  arn      = aws_sfn_state_machine.daily.arn
  role_arn = aws_iam_role.daily_schedule.arn
}

resource "aws_cloudwatch_metric_alarm" "daily_failed" {
  alarm_name          = "${var.name}-daily-failed"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.daily.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_iam_role_policy" "receiver_mail" {
  role = aws_iam_role.receiver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:PutItem"
      Resource = aws_dynamodb_table.profiles.arn
    }]
  })
}

resource "aws_iam_role_policy" "feedback_registrations" {
  role = aws_iam_role.lambda["nba-feedback"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:PutItem"
      Resource = aws_dynamodb_table.profiles.arn
    }]
  })
}

resource "aws_iam_role_policy" "advance_registrations" {
  role = aws_iam_role.lambda["daily-advance"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:UpdateItem"
      Resource = aws_dynamodb_table.profiles.arn
    }]
  })
}

resource "aws_iam_role_policy" "export_daily" {
  role = aws_iam_role.export.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "dynamodb:GetItem"
        Resource = aws_dynamodb_table.profiles.arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.raw.arn}/analytics/daily/*"
      },
    ]
  })
}

output "daily_state_machine_arn" {
  value = aws_sfn_state_machine.daily.arn
}
