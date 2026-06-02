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

  # Self-termination only. The Name tag condition restricts the action
  # to instances bearing the developer-worker tag. The worker cannot
  # accidentally terminate an instance in prog-strength-infra.
  statement {
    sid       = "TerminateSelf"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Name"
      values   = ["prog-strength-developer-worker"]
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

# --------------------------------------------------------------------
# GitHub Actions OIDC role: assumed by the dispatch-sow workflow.
# Trust restricted to a single repo + branch so feature-branch runs
# cannot accidentally provision resources.
# --------------------------------------------------------------------

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_actions_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_actions_repo}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "prog-strength-developer-github-actions-role"
  assume_role_policy = data.aws_iam_policy_document.github_actions_trust.json
}

data "aws_iam_policy_document" "github_actions_inline" {
  # Terraform read/apply on the developer's resources.
  statement {
    sid = "EC2Manage"
    actions = [
      "ec2:Describe*",
      "ec2:RunInstances",
      "ec2:CreateTags",
      "ec2:TerminateInstances",
      "ec2:CreateLaunchTemplate",
      "ec2:CreateLaunchTemplateVersion",
      "ec2:DeleteLaunchTemplate",
      "ec2:ModifyLaunchTemplate",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassWorkerRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.worker.arn]
  }

  statement {
    sid = "IAMRead"
    actions = [
      "iam:GetRole",
      "iam:GetInstanceProfile",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      # OIDC provider lookup for the github_actions_trust data source.
      # Required so Terraform refresh can resolve the existing provider.
      "iam:ListOpenIDConnectProviders",
      "iam:GetOpenIDConnectProvider",
    ]
    resources = ["*"]
  }

  # Terraform state in S3 + optional DynamoDB lock are configured
  # outside this Terraform via `terraform init -backend-config=...`,
  # but the GHA role still needs the permission to read/write them.
  # Bucket name is owner-specific; permission granted on all S3 here
  # and audited via CloudTrail. Tighten later if cross-tenant.
  statement {
    sid = "S3StateBackend"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = ["*"]
  }

  statement {
    sid = "VPCRead"
    actions = [
      "ec2:DescribeVpcs",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeInternetGateways",
      "ec2:DescribeRouteTables",
    ]
    resources = ["*"]
  }

  # CloudWatch Logs read — required for Terraform to refresh the
  # log group resource on every plan. No write permissions because
  # the log group is created locally via admin credentials.
  statement {
    sid = "CloudWatchLogsRead"
    actions = [
      "logs:DescribeLogGroups",
      "logs:ListTagsForResource",
      "logs:ListTagsLogGroup",
    ]
    resources = ["*"]
  }

  # SSM parameter read — for the al2023_ami AMI lookup. The
  # `/aws/service/ami-*` parameter namespace is AWS-published and
  # accessible to any role with this action.
  statement {
    sid = "SSMParameterRead"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      "arn:aws:ssm:${var.aws_region}::parameter/aws/service/ami-amazon-linux-latest/*",
    ]
  }

  # Secrets Manager describe — required for the two
  # `data "aws_secretsmanager_secret"` blocks to refresh on each plan.
  # GetResourcePolicy is read by the same data source even though we
  # don't set a resource policy on either secret. Scoped to the
  # developer namespace; the GHA role never needs to read the actual
  # secret values (the worker EC2 does, via its own role and
  # secretsmanager:GetSecretValue).
  statement {
    sid = "SecretsManagerDescribe"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
    ]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:prog-strength-developer/*",
    ]
  }
}

resource "aws_iam_role_policy" "github_actions_inline" {
  name   = "prog-strength-developer-github-actions-inline"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_inline.json
}
