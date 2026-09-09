# The AWS provider pointed at LocalStack.
#
# The same resource definitions are what would run against real AWS; only this provider block
# changes. Credentials here are the conventional LocalStack dummies and grant nothing: real
# credentials never appear in Terraform files, they come from the pod's IAM role in production.

provider "aws" {
  region = var.aws_region

  access_key = "test"
  secret_key = "test"

  # LocalStack has no IMDS, no STS identity to look up, and serves buckets path-style on
  # localhost. Without these the provider spends its time failing to reach real AWS endpoints.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    s3  = var.localstack_endpoint
    sqs = var.localstack_endpoint
    iam = var.localstack_endpoint
    sts = var.localstack_endpoint
  }

  default_tags {
    tags = {
      Project     = "convoscore"
      Environment = "local"
      ManagedBy   = "terraform"
    }
  }
}
