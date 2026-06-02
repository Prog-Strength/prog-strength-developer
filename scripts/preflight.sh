#!/usr/bin/env bash
# preflight.sh — fail the workflow fast if a worker is already running.
# Called from .github/workflows/dispatch-sow.yml before `terraform apply`.
#
# Single-instance concurrency is the v1 model. A second dispatch while a
# worker is alive returns non-zero with a clear message; the workflow
# fails before any AWS state changes.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"

running=$(aws ec2 describe-instances \
  --region "$REGION" \
  --filters \
    'Name=tag:Name,Values=prog-strength-developer-worker' \
    'Name=instance-state-name,Values=pending,running' \
  --query 'Reservations[].Instances[].InstanceId' \
  --output text)

if [ -n "$running" ]; then
  echo "::error::A prog-strength-developer worker is already running: $running"
  echo "::error::v1 enforces single-instance concurrency. Wait for the current run to terminate."
  exit 1
fi

echo "preflight: no in-flight worker; OK to proceed"
