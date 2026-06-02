# Single log group for all worker instances. Each instance writes its
# own log stream named after the instance ID (config in userdata).
resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/ec2/prog-strength-developer"
  retention_in_days = var.log_retention_days
}
