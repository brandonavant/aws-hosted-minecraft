terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    # All values supplied via -backend-config=backend-configs/prod.hcl on `terraform init`.
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.44.0"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.5.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
}
