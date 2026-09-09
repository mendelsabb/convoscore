# The scoring queue and its dead-letter queue.
#
# Only job ids travel through here. PostgreSQL holds the conversation, the result and the state,
# so a lost or duplicated message costs a redelivery, never data.

resource "aws_sqs_queue" "scoring_dlq" {
  name = "${var.name_prefix}-scoring-dlq"

  # Failed jobs are already recorded in PostgreSQL with their error, so the queue copy is kept
  # only long enough to investigate a crash-looping consumer.
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "scoring" {
  name = "${var.name_prefix}-scoring"

  visibility_timeout_seconds = var.visibility_timeout_seconds

  # Long polling: the worker waits for work rather than spinning on empty receives.
  receive_wait_time_seconds = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.scoring_dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}
