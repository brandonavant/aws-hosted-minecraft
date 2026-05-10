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

variable "domain_zone" {
  description = "Apex domain of the Route 53 hosted zone that already exists in this account (e.g. \"example.com\"). The zone is looked up via a data source; this stack does not create it."
  type        = string
}

variable "mc_subdomain" {
  description = "Subdomain label under domain_zone for the Minecraft A record. The full FQDN becomes \"<mc_subdomain>.<domain_zone>\"."
  type        = string
  default     = "mc"
}

variable "ssh_allowed_cidrs" {
  description = "IPv4 CIDRs allowed to reach SSH (port 22), in addition to AWS's lightsail-connect range. When empty, the operator's current public IP is auto-detected via api.ipify.org and pinned as a /32."
  type        = list(string)
  default     = []
}
