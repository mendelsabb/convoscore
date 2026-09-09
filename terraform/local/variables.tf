variable "aws_region" {
  description = "Region used for all resources. LocalStack echoes the client region back, so keep it consistent everywhere."
  type        = string
  default     = "us-east-1"
}

variable "localstack_endpoint" {
  description = "LocalStack edge endpoint, reachable from the host via the kind port mapping."
  type        = string
  default     = "http://127.0.0.1:4566"
}

variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "convoscore"
}

variable "incoming_prefix" {
  description = "Bucket prefix the ingestor reads. The ingestor's IAM policy is scoped to it."
  type        = string
  default     = "incoming/"
}

variable "visibility_timeout_seconds" {
  description = "How long a claimed message stays hidden. Must exceed the LLM timeout plus database work, or SQS would redeliver a message that is still being processed."
  type        = number
  default     = 90
}

variable "max_receive_count" {
  description = "Deliveries before a message is moved to the dead-letter queue. Deliberately higher than the application's own attempt cap (3), so the DLQ only catches consumers that crash before they can record anything."
  type        = number
  default     = 5
}
