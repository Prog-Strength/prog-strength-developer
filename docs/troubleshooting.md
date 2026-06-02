# Troubleshooting

## "Worker dispatched but no PRs ever appeared"

Order of operations:

1. **Check if the EC2 actually terminated.**
   ```bash
   aws ec2 describe-instances \
     --filters 'Name=tag:Name,Values=prog-strength-developer-worker' \
               'Name=instance-state-name,Values=pending,running' \
     --query 'Reservations[].Instances[].[InstanceId,LaunchTime]' \
     --output table
   ```
   If one's still alive, either the work is in progress (check CloudWatch)
   or the backstop hasn't fired yet.

2. **Read CloudWatch logs.** Find the instance ID from the GitHub Actions
   summary, then:
   ```bash
   aws logs tail /aws/ec2/prog-strength-developer \
     --log-stream-names <instance-id> \
     --since 1h
   ```
   Scan the last ~200 lines. Common failure modes are listed below.

3. **SSH (well, SSM) into the box** if it's still alive:
   ```bash
   aws ssm start-session --target <instance-id>
   ```

## Common failure modes

### "Bootstrap dies fetching secrets"

CloudWatch shows `AccessDeniedException` from Secrets Manager. Either:

- The secret doesn't exist yet (run setup step 8).
- The worker IAM role doesn't have permission. Check
  `terraform/iam.tf`'s `ReadDeveloperSecrets` statement; the resource
  ARN pattern must match the secret's actual ARN (Secrets Manager appends
  a random suffix; the pattern uses `*` to match it).

### "GitHub App token mint fails with 401"

CloudWatch shows the Python urllib snippet failing on the
`/access_tokens` call. Causes:

- App ID or installation ID in the secret is wrong. Re-check the App's
  settings page and the install URL.
- Private key in the secret is malformed (missing newlines, wrong PEM
  header, etc.). The `jq -Rs .` step in setup converts a PEM file to a
  JSON-safe string; if you skipped it, the literal newlines will have
  broken the JSON.
- App is installed on the org but lacks the necessary repository
  permissions. Re-check the App configuration: Contents (write), Pull
  Requests (write), Workflows (write).

### "Claude login fails with 401"

The OAuth refresh token in `~/.claude/credentials.json` has expired (or
the file in Secrets Manager is stale). Re-run `claude login` locally,
copy the new credentials.json, and re-seed the secret:

```bash
aws secretsmanager put-secret-value \
  --secret-id prog-strength-developer/claude-credentials \
  --secret-string "$(cat ~/.claude/credentials.json)"
```

Refresh tokens last several months; expect to do this 2–4× per year.

### "PRs opened but worker still running"

Either Claude wrote PRs but didn't exit cleanly (the 6h backstop will
eventually fire), or the script is stuck on a downstream step. SSM in
and check `ps -ef | grep claude` and `tail -f /var/log/prog-strength-developer/*.log`.

### "Workflow fails on preflight check"

Output says "A prog-strength-developer worker is already running:
<instance-id>". This is by design — v1 is single-instance. Either wait
for the current worker to terminate or, if you know it's stuck, manually
terminate:

```bash
aws ec2 terminate-instances --instance-ids <instance-id>
```

Then re-dispatch.

### "Terraform apply fails with 'OIDC provider does not exist'"

You skipped setup step 3. Create the provider:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

### "Terraform plan wants to destroy and recreate the launch template every run"

Expected. Each `terraform apply` from the workflow renders the userdata
template with the new `sow_path` baked in, which changes the launch
template's user_data, which Terraform considers a forced replacement.
The launch template recreation is cheap; the previously-launched
instances (if any) are not affected.

## "I want to nuke everything and start over"

```bash
cd terraform
terraform destroy
```

Then manually delete the S3 state bucket, the two Secrets Manager entries,
and the OIDC provider. The GitHub App can stay; just uninstall it from the
org if you want it gone. (No DynamoDB table to clean up — locking is via
the S3 `use_lockfile` native mode.)

## "CloudWatch stream is completely empty for an instance that ran"

The CloudWatch agent is installed and configured ~45 lines into the
userdata script. Failures during the earlier dependency-install phase
(e.g., a `dnf install` glitch) terminate the instance before the agent
ever ships a byte to CloudWatch.

Two recourses:

1. **If the instance is still alive** (rare — the ERR trap usually
   terminates it within seconds): SSM in and read
   `/var/log/cloud-init-output.log` and `/var/log/prog-strength-developer/userdata.log`
   directly. The userdata writes everything to those local files via the
   `exec > >(tee)` redirect, so failure context is captured even when
   nothing reaches CloudWatch.
2. **If the instance is gone:** there's no remote log to read. Re-dispatch
   with the same SOW and watch CloudWatch live (the agent will start
   shipping bytes once it gets past `systemctl enable --now amazon-cloudwatch-agent`).
   If the failure is reproducible at the same point, that's the diagnostic
   evidence.

A future hardening pass could pre-install + start the CloudWatch agent
via a custom AMI, closing this gap entirely.

## "How do I see what Claude was thinking on a failed run?"

CloudWatch retains 30 days of logs. The instance's log stream is named
exactly the instance ID, viewable in the AWS Console under CloudWatch
Logs → /aws/ec2/prog-strength-developer.

Within the stream:

- `[userdata ...]` lines are progress markers from `userdata.sh.tpl`.
- Plain lines (no prefix) are Claude's stdout — its thoughts, tool calls,
  and subagent dispatches.

The last 50–100 lines are usually where the failure mode is most
diagnostic.
