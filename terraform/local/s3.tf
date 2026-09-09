# The conversation bucket: the second ingestion path.
#
# Objects dropped under the incoming prefix are turned into the same jobs the API creates.

resource "aws_s3_bucket" "conversations" {
  bucket = "${var.name_prefix}-conversations"

  # Local only, so the bucket can be destroyed with objects still in it.
  force_destroy = true
}

# Conversation transcripts are customer data. Even locally the bucket is private, so the demo
# never models a public bucket.
resource "aws_s3_bucket_public_access_block" "conversations" {
  bucket = aws_s3_bucket.conversations.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "conversations" {
  bucket = aws_s3_bucket.conversations.id

  versioning_configuration {
    # Off locally: the demo re-uploads the same keys, and versioning would make the bucket
    # listing harder to reason about. Production would enable it for recoverability, alongside
    # the retention policy discussed in the production architecture document.
    status = "Suspended"
  }
}
