# Dedicated VPC for the autonomous developer. Explicitly separate from
# the application VPC in prog-strength-infra. No peering, no shared
# route tables — a misbehaving worker cannot route to the prod stack.

resource "aws_vpc" "developer" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "prog-strength-developer-vpc"
  }
}

# One public subnet. Single AZ is fine for ephemeral compute — there is
# no high-availability requirement when each worker exists for hours.
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.developer.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = true

  tags = {
    Name = "prog-strength-developer-public"
  }
}

resource "aws_internet_gateway" "developer" {
  vpc_id = aws_vpc.developer.id

  tags = {
    Name = "prog-strength-developer-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.developer.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.developer.id
  }

  tags = {
    Name = "prog-strength-developer-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Worker security group: no inbound (SSM Session Manager handles
# debugging), all outbound (long tail of package registries, the
# Anthropic API, GitHub, AWS, arbitrary URLs Claude may fetch).
# Trust boundary is the VPC isolation + IAM role, not egress filtering.
resource "aws_security_group" "worker" {
  name        = "prog-strength-developer-worker-sg"
  description = "Autonomous developer worker - outbound only, no SSH."
  vpc_id      = aws_vpc.developer.id

  egress {
    description = "All outbound - worker needs broad internet access."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "prog-strength-developer-worker-sg"
  }
}

