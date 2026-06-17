# Permanent developer manager: hosts Prometheus + Grafana + Pushgateway +
# Caddy + cAdvisor + node_exporter (+ Loki, stretch). Sits in the
# existing developer VPC in a separate public subnet from workers so
# the SG boundary between manager and worker traffic is explicit.

# Second public subnet, sharing the existing route table (the IGW route
# is already attached to that table).
resource "aws_subnet" "manager_public" {
  vpc_id                  = aws_vpc.developer.id
  cidr_block              = var.manager_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "prog-strength-developer-manager-public"
  }
}

resource "aws_route_table_association" "manager_public" {
  subnet_id      = aws_subnet.manager_public.id
  route_table_id = aws_route_table.public.id
}

# Manager security group: 80/443 from the world (Caddy + Let's Encrypt
# HTTP-01), 9091 from workers (Pushgateway final-state push). 3100
# (Loki) is added later in the stretch goal. All outbound open.
resource "aws_security_group" "manager" {
  name        = "prog-strength-developer-manager-sg"
  description = "Developer manager - Caddy public, Pushgateway from worker SG."
  vpc_id      = aws_vpc.developer.id

  ingress {
    description = "HTTPS (Caddy)."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP for Caddy redirect to HTTPS plus Lets Encrypt HTTP-01."
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description     = "Pushgateway from workers."
    from_port       = 9091
    to_port         = 9091
    protocol        = "tcp"
    security_groups = [aws_security_group.worker.id]
  }

  egress {
    description = "All outbound."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "prog-strength-developer-manager-sg"
  }
}

# Reciprocal worker SG rules: workers must accept scrapes from the
# manager (9100 node_exporter, 9101 worker_exporter). The worker SG
# itself lives in vpc.tf; the rules are added here as standalone
# resources so vpc.tf stays unchanged.
resource "aws_security_group_rule" "worker_ingress_node_exporter" {
  type                     = "ingress"
  from_port                = 9100
  to_port                  = 9100
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.manager.id
  security_group_id        = aws_security_group.worker.id
  description              = "node_exporter scrape from manager."
}

resource "aws_security_group_rule" "worker_ingress_worker_exporter" {
  type                     = "ingress"
  from_port                = 9101
  to_port                  = 9101
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.manager.id
  security_group_id        = aws_security_group.worker.id
  description              = "worker_exporter scrape from manager."
}

# --------------------------------------------------------------------
# Manager IAM. SSM (ops access), CloudWatch logs (bootstrap log),
# Secrets Manager read on the github-app secret (so userdata can clone
# the private prog-strength-developer repo with a fresh installation
# token), and ec2:Describe* so Prometheus' ec2_sd_config can enumerate
# worker targets at scrape time.
# --------------------------------------------------------------------
data "aws_iam_policy_document" "manager_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "manager" {
  name               = "prog-strength-developer-manager-role"
  assume_role_policy = data.aws_iam_policy_document.manager_trust.json
}

resource "aws_iam_role_policy_attachment" "manager_ssm" {
  role       = aws_iam_role.manager.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "manager_inline" {
  statement {
    sid     = "ReadGithubAppSecret"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:prog-strength-developer/github-app-*",
    ]
  }

  # ec2:Describe* on '*' is required for ec2_sd_config to enumerate
  # worker instances; AWS scopes Describe at the API level, not by
  # resource. Read-only.
  statement {
    sid       = "EC2DescribeForSD"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeAvailabilityZones"]
    resources = ["*"]
  }

  # The ddb_exporter service scans the run-history table to publish
  # aggregate metrics. Read-only, scoped to that one table.
  statement {
    sid       = "ScanRunHistory"
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.runs.arn]
  }
}

resource "aws_iam_role_policy" "manager_inline" {
  name   = "prog-strength-developer-manager-inline"
  role   = aws_iam_role.manager.id
  policy = data.aws_iam_policy_document.manager_inline.json
}

resource "aws_iam_instance_profile" "manager" {
  name = "prog-strength-developer-manager-profile"
  role = aws_iam_role.manager.name
}

# --------------------------------------------------------------------
# Data volume + instance + EIP.
# --------------------------------------------------------------------
resource "aws_ebs_volume" "manager_data" {
  availability_zone = var.availability_zone
  size              = var.manager_data_volume_size_gb
  type              = "gp3"

  tags = {
    Name = "prog-strength-developer-manager-data"
  }
}

# AL2023 arm64 AMI for the Graviton t4g.small.
data "aws_ssm_parameter" "al2023_arm64_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  manager_userdata = templatefile("${path.module}/../bootstrap/manager_userdata.sh.tpl", {
    aws_region             = var.aws_region
    github_org             = var.github_org
    github_app_secret_name = data.aws_secretsmanager_secret.github_app.name
    grafana_admin_user     = var.grafana_admin_user
    grafana_admin_password = var.grafana_admin_password
  })
}

resource "aws_instance" "manager" {
  ami                    = data.aws_ssm_parameter.al2023_arm64_ami.value
  instance_type          = var.manager_instance_type
  iam_instance_profile   = aws_iam_instance_profile.manager.name
  subnet_id              = aws_subnet.manager_public.id
  vpc_security_group_ids = [aws_security_group.manager.id]

  # Docker containers reach IMDS for the role credentials; bump the hop
  # limit to 2 so requests through the docker bridge still resolve.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  user_data = base64gzip(local.manager_userdata)

  tags = {
    Name = "prog-strength-developer-manager"
  }

  # AMI bumps land via a deliberate replace, not drift. Userdata is
  # only re-rendered for instance replacement; live updates flow
  # through deploy-manager.yml over SSM.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "manager_data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.manager_data.id
  instance_id                    = aws_instance.manager.id
  stop_instance_before_detaching = true
}

resource "aws_eip" "manager" {
  instance = aws_instance.manager.id
  domain   = "vpc"

  tags = {
    Name = "prog-strength-developer-manager-eip"
  }
}

# --------------------------------------------------------------------
# Stretch goal SG rules: workers push Promtail logs to Loki on :3100.
# The worker SG's broad egress already permits this; the explicit
# rule documents intent and survives any future tightening of egress.
# --------------------------------------------------------------------
resource "aws_security_group_rule" "manager_ingress_loki" {
  type                     = "ingress"
  from_port                = 3100
  to_port                  = 3100
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.worker.id
  security_group_id        = aws_security_group.manager.id
  description              = "Loki push from workers (stretch goal)."
}
