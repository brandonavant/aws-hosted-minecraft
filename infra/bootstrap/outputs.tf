output "state_bucket_name" {
  description = "S3 bucket holding Terraform remote state for the main layer. Paste into infra/backend-configs/prod.hcl as `bucket`."
  value       = aws_s3_bucket.tfstate.bucket
}

output "lock_table_name" {
  description = "DynamoDB table for Terraform state locks. Paste into infra/backend-configs/prod.hcl as `dynamodb_table`."
  value       = aws_dynamodb_table.tfstate_locks.name
}

output "region" {
  description = "AWS region the state bucket and lock table were created in. Paste into infra/backend-configs/prod.hcl as `region`."
  value       = var.region
}
