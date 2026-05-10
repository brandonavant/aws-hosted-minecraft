variable "region" {
  description = "AWS region for the remote-state backend resources. Must match the region the main infra/ layer targets."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile name configured via IAM Identity Center (matches MC_AWS_PROFILE in .env)."
  type        = string
  default     = "mc-aws"
}

variable "state_bucket_name" {
  description = <<-EOT
    Globally unique S3 bucket name for Terraform remote state. S3 bucket names share a global namespace,
    so pick something that is unlikely to collide (e.g. "mc-aws-tfstate-<random-suffix>"). The same value
    must appear in infra/backend-configs/prod.hcl for the main layer.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name (lowercase, 3-63 chars, no underscores)."
  }
}

variable "lock_table_name" {
  description = "DynamoDB table name for Terraform state locks. Account-and-region-scoped, so collisions are local."
  type        = string
  default     = "mc-aws-tfstate-locks"
}

variable "tags" {
  description = "Tags applied to every resource the bootstrap layer manages."
  type        = map(string)
  default = {
    Project   = "aws-hosted-minecraft"
    Component = "tf-bootstrap"
    ManagedBy = "terraform"
  }
}
