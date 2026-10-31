output "events_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/v1/events"
}

output "api_key_id" {
  description = "Read the value with: aws apigateway get-api-key --include-value --api-key <id>"
  value       = aws_api_gateway_api_key.default.id
}

output "receiver_url" {
  value = aws_apigatewayv2_stage.receiver.invoke_url
}

output "receiver_table" {
  value = aws_dynamodb_table.receiver.name
}

output "nba_scan_function" {
  value = aws_lambda_function.fn["nba-scan"].function_name
}

output "nba_retrain_function" {
  value = aws_lambda_function.fn["nba-retrain"].function_name
}

output "raw_bucket" {
  value = aws_s3_bucket.raw.bucket
}

output "profiles_table" {
  value = aws_dynamodb_table.profiles.name
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}
