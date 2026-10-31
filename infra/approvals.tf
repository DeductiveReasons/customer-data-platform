# ---------- Approval status for message variations ----------

resource "aws_dynamodb_table" "approvals" {
  name         = "${var.name}-variation-approvals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "variation_id"

  attribute {
    name = "variation_id"
    type = "S"
  }
  server_side_encryption {
    enabled = true
  }
}

resource "aws_iam_role_policy" "decide_approvals" {
  role = aws_iam_role.lambda["nba-decide"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:Scan"
      Resource = aws_dynamodb_table.approvals.arn
    }]
  })
}

resource "aws_iam_role_policy" "export_approvals" {
  role = aws_iam_role.export.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:Scan"
      Resource = aws_dynamodb_table.approvals.arn
    }]
  })
}

resource "aws_iam_role_policy" "webapp_approvals" {
  role = aws_iam_role.webapp.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:Scan", "dynamodb:PutItem"]
      Resource = aws_dynamodb_table.approvals.arn
    }]
  })
}
