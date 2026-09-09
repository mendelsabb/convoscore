# These outputs are consumed by scripts/up.sh and passed into the Helm release, so the
# application uses exactly the resources Terraform created. Nothing here is decorative.

output "queue_name" {
  description = "Scoring queue name. The application resolves the URL from this at runtime."
  value       = aws_sqs_queue.scoring.name
}

output "queue_url" {
  description = "Scoring queue URL, for scripts and debugging."
  value       = aws_sqs_queue.scoring.url
}

output "dlq_name" {
  description = "Dead-letter queue name."
  value       = aws_sqs_queue.scoring_dlq.name
}

output "bucket_name" {
  description = "Conversation bucket the ingestor reads."
  value       = aws_s3_bucket.conversations.bucket
}

output "incoming_prefix" {
  description = "Prefix under which conversations are picked up."
  value       = var.incoming_prefix
}

output "component_role_arns" {
  description = "Per-component IAM roles. Not enforced by LocalStack; documented in DECISIONS.md."
  value       = { for name, role in aws_iam_role.component : name => role.arn }
}
