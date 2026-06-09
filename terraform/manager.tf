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
    description = "HTTP -> HTTPS redirect (Caddy + Let's Encrypt HTTP-01)."
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
