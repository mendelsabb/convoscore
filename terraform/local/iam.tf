# Least privilege, one role per component.
#
# Be clear about what this is: LocalStack creates these roles and policies but does not enforce
# them, so locally they are a demonstration rather than a control. They are here because they are
# the exact documents production would apply, and writing them now is what makes the production
# story concrete rather than aspirational. See DECISIONS.md section 12.
#
# Each component gets only the actions it uses:
#   api      - put a job id on the queue
#   worker   - consume from the queue and manage message visibility
#   ingestor - read the incoming prefix, and put a job id on the queue
#
# Nothing has permission to delete objects, delete queues, or read anything outside the prefix.

locals {
  components = {
    api = {
      statements = [
        {
          sid       = "EnqueueScoringJobs"
          actions   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
          resources = [aws_sqs_queue.scoring.arn]
        },
      ]
    }

    worker = {
      statements = [
        {
          sid = "ConsumeScoringJobs"
          actions = [
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            # Retry backoff is applied by extending a message's visibility.
            "sqs:ChangeMessageVisibility",
            "sqs:GetQueueUrl",
            "sqs:GetQueueAttributes",
          ]
          resources = [aws_sqs_queue.scoring.arn]
        },
        {
          sid       = "ReadDeadLetterQueueDepth"
          actions   = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
          resources = [aws_sqs_queue.scoring_dlq.arn]
        },
      ]
    }

    ingestor = {
      statements = [
        {
          sid       = "ReadIncomingConversations"
          actions   = ["s3:GetObject"]
          resources = ["${aws_s3_bucket.conversations.arn}/${var.incoming_prefix}*"]
        },
        {
          sid       = "ListIncomingPrefix"
          actions   = ["s3:ListBucket"]
          resources = [aws_s3_bucket.conversations.arn]
        },
        {
          sid       = "EnqueueScoringJobs"
          actions   = ["sqs:SendMessage", "sqs:GetQueueUrl", "sqs:GetQueueAttributes"]
          resources = [aws_sqs_queue.scoring.arn]
        },
      ]
    }
  }
}

# Trust policy shaped for EKS Pod Identity, which is how these roles would be assumed in
# production: no long-lived access keys anywhere.
data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    sid     = "AllowEksPodIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "component" {
  for_each = local.components

  dynamic "statement" {
    for_each = each.value.statements

    content {
      sid       = statement.value.sid
      effect    = "Allow"
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }
}

# The ingestor may list the bucket, but only within the incoming prefix.
data "aws_iam_policy_document" "ingestor_scoped" {
  source_policy_documents = [data.aws_iam_policy_document.component["ingestor"].json]

  statement {
    sid       = "DenyListingOutsideIncomingPrefix"
    effect    = "Deny"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.conversations.arn]

    condition {
      test     = "StringNotLike"
      variable = "s3:prefix"
      values   = ["${var.incoming_prefix}*", var.incoming_prefix]
    }
  }
}

resource "aws_iam_policy" "component" {
  for_each = local.components

  name        = "${var.name_prefix}-${each.key}"
  description = "Least-privilege policy for the ConvoScore ${each.key} component."
  policy = (
    each.key == "ingestor"
    ? data.aws_iam_policy_document.ingestor_scoped.json
    : data.aws_iam_policy_document.component[each.key].json
  )
}

resource "aws_iam_role" "component" {
  for_each = local.components

  name               = "${var.name_prefix}-${each.key}"
  description        = "Role assumed by the ConvoScore ${each.key} pods."
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "component" {
  for_each = local.components

  role       = aws_iam_role.component[each.key].name
  policy_arn = aws_iam_policy.component[each.key].arn
}
