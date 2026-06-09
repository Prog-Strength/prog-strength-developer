# Latest Amazon Linux 2023 AMI (x86_64) for the worker.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# Launch template only — worker instances are launched ad-hoc by the
# dispatch-sow workflow via `aws ec2 run-instances` with a SOW-specific
# userdata override. Keeping the instance out of Terraform is what
# unblocks concurrent dispatches: there is no shared state lock to race.
#
# The base userdata embedded here uses an empty sow_path placeholder;
# the dispatch workflow ALWAYS overrides via `--user-data` at run time,
# so this template's baked userdata never runs in production. It exists
# so the launch template remains a valid standalone resource that can
# also be tested by hand if needed.
locals {
  base_userdata = templatefile("${path.module}/../bootstrap/userdata.sh.tpl", {
    aws_region             = var.aws_region
    sow_path               = ""
    github_org             = var.github_org
    log_group_name         = aws_cloudwatch_log_group.worker.name
    max_runtime_hours      = var.max_runtime_hours
    claude_secret_name     = data.aws_secretsmanager_secret.claude_credentials.name
    github_app_secret_name = data.aws_secretsmanager_secret.github_app.name
    manager_private_ip     = "" # overridden by the workflow render
  })
}

resource "aws_launch_template" "worker" {
  name          = "prog-strength-developer-worker"
  image_id      = data.aws_ssm_parameter.al2023_ami.value
  instance_type = var.instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  network_interfaces {
    associate_public_ip_address = true
    security_groups             = [aws_security_group.worker.id]
    subnet_id                   = aws_subnet.public.id
  }

  # IMDSv2 only.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64gzip(local.base_userdata)

  # Per-instance tags (Name, SOW) are applied by the dispatch workflow
  # via --tag-specifications, not here — so concurrent workers can each
  # carry their own SOW path without launch-template churn.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "prog-strength-developer-worker"
    }
  }
}
