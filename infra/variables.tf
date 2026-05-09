variable "region" {
  description = "AWS region for the LightSail deployment. Must match the bootstrap layer's region."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile name configured via IAM Identity Center (matches MC_AWS_PROFILE in .env)."
  type        = string
  default     = "mc-aws"
}
