# Latest Amazon Linux 2023 AMI in the region, looked up via SSM
# parameter. SSM gives us the canonical "latest" pointer without
# pinning to an AMI ID that goes stale within weeks.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# Render the userdata template with the runtime variables baked in.
# templatefile() substitutes ${var} placeholders inside userdata.sh.tpl.
#
# The Claude prompt template is NOT inlined here — instead userdata
# clones prog-strength-developer at boot and reads bootstrap/prompt.md.tpl
# from that working copy. This keeps the rendered userdata under EC2's
# 16KB user_data limit and means iterating on the prompt doesn't
# require a launch-template replacement.
locals {
  userdata = templatefile("${path.module}/../bootstrap/userdata.sh.tpl", {
    aws_region             = var.aws_region
    sow_path               = var.sow_path
    github_org             = var.github_org
    log_group_name         = aws_cloudwatch_log_group.worker.name
    max_runtime_hours      = var.max_runtime_hours
    claude_secret_name     = data.aws_secretsmanager_secret.claude_credentials.name
    github_app_secret_name = data.aws_secretsmanager_secret.github_app.name
  })
}

resource "aws_launch_template" "worker" {
  name_prefix   = "prog-strength-developer-worker-"
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

  # IMDSv2 only. Required for the worker's self-termination flow (which
  # queries the instance ID via the IMDS token endpoint) and good
  # hygiene regardless.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(local.userdata)

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "prog-strength-developer-worker"
      SOW  = var.sow_path
    }
  }
}

# The actual instance: created ONLY when sow_path is non-empty. The
# workflow_dispatch wrapper passes sow_path; manual `terraform apply`
# without it produces only the launch template + persistent infra.
#
# count = 1 when dispatching; count = 0 for state-only applies.
resource "aws_instance" "worker" {
  count = var.sow_path != "" ? 1 : 0

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  # Explicit dependency so the IAM role's inline policy is attached
  # BEFORE the instance boots and starts hitting Secrets Manager.
  depends_on = [aws_iam_role_policy.worker_inline]

  tags = {
    Name = "prog-strength-developer-worker"
    SOW  = var.sow_path
  }
}
