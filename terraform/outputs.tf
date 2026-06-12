output "vpc_id" {
  description = "Developer VPC ID."
  value       = aws_vpc.developer.id
}

output "public_subnet_id" {
  description = "Public subnet hosting the worker."
  value       = aws_subnet.public.id
}

output "worker_security_group_id" {
  description = "Security group ID for the worker."
  value       = aws_security_group.worker.id
}

output "worker_role_arn" {
  description = "IAM role assumed by the worker via its instance profile."
  value       = aws_iam_role.worker.arn
}

output "log_group_name" {
  description = "CloudWatch log group for worker output."
  value       = aws_cloudwatch_log_group.worker.name
}

# worker_instance_id removed: workers are launched directly by the
# dispatch-sow workflow via aws ec2 run-instances and no longer live in
# Terraform state. The dispatch workflow's summary surfaces the
# instance ID it created.

output "manager_instance_id" {
  description = "Instance ID of the permanent developer manager. Use with `aws ssm start-session --target ...` for ops access."
  value       = aws_instance.manager.id
}

output "manager_public_ip" {
  description = "Elastic IP of the developer manager. Paste into GoDaddy as the A record for developers.progstrength.fitness."
  value       = aws_eip.manager.public_ip
}

output "manager_security_group_id" {
  description = "Security group ID for the manager."
  value       = aws_security_group.manager.id
}
