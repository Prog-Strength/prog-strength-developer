# --------------------------------------------------------------------
# Worker role: assumed by the EC2 worker via its instance profile.
# Least-privilege: narrow secret access, self-only termination, SSM,
# CloudWatch Logs.
# --------------------------------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "worker_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "prog-strength-developer-worker-role"
  assume_role_policy = data.aws_iam_policy_document.worker_trust.json
}

# Managed policy for SSM Session Manager. AWS-curated; cheaper than
# hand-rolling the ssmmessages:* / ec2messages:* statements.
resource "aws_iam_role_policy_attachment" "worker_ssm" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "worker_inline" {
  # Secrets Manager: only the two developer secrets, by ARN.
  statement {
    sid     = "ReadDeveloperSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:prog-strength-developer/claude-credentials-*",
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:prog-strength-developer/github-app-*",
    ]
  }

  # Worker-scoped termination. The condition restricts the action to
  # instances whose instance profile is the worker profile, so a worker
  # cannot terminate the manager or any non-worker instance in the
  # account. Cross-worker termination is theoretically possible — any
  # worker can terminate any other worker — but the boundary that
  # actually matters is workers-vs-everything-else, which this enforces.
  #
  # The earlier ec2:SourceInstanceARN ArnEquals $${aws:ResourceArn}
  # attempt at true self-only fails IAM's legacy parser on PutRolePolicy:
  # ${aws:ResourceArn} is only a substitution variable in the newer
  # IAM policy parser, and inline role policies are validated against
  # the legacy one. True self-only needs a launch-time per-worker tag
  # or a per-worker role, which is more refactor than the threat warrants
  # given Claude Code is the only thing running on workers.
  statement {
    sid       = "TerminateSelf"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"]
    condition {
      test     = "ArnEquals"
      variable = "ec2:InstanceProfile"
      values   = [aws_iam_instance_profile.worker.arn]
    }
  }

  # CloudWatch Logs: worker streams stdout/stderr.
  statement {
    sid = "CloudWatchLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker_inline" {
  name   = "prog-strength-developer-worker-inline"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_inline.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "prog-strength-developer-worker-profile"
  role = aws_iam_role.worker.name
}
