# Troubleshooting

## Dispatching a SOW (important — read before each dispatch)

**Use the `dispatch-sow` shell function below, not the GitHub UI's
"Run workflow" button.** This is the load-bearing operational practice
for the autonomous developer because of how Claude Code OAuth tokens
rotate.

### The OAuth rotation problem

The Claude OAuth refresh token in Secrets Manager goes stale every
time you use Claude Code locally — Anthropic invalidates the old
refresh token the moment a new one is minted (a standard one-time-use
refresh-token pattern). If you dispatch a worker, then use local
Claude Code in between extraction and dispatch, the worker boots with
already-rotated credentials and dies with `Failed to authenticate.
API Error: 401 Invalid authentication credentials` at the claude
invocation.

The window of vulnerability is essentially "any time you use Claude
Code on your laptop after the last re-seed." The cure is to make
re-seeding part of every dispatch.

### The fix: dispatch-sow shell function

Add this to `~/.zshrc` (or `~/.bashrc`):

```bash
dispatch-sow() {
  local sow_path="${1:?usage: dispatch-sow <sow-path>  (e.g. sows/foo.md)}"
  echo "==> Re-seeding Claude credentials from Keychain"
  aws secretsmanager put-secret-value \
    --region us-east-2 \
    --secret-id prog-strength-developer/claude-credentials \
    --secret-string "$(security find-generic-password -s 'Claude Code-credentials' -a "$USER" -w)" \
    > /dev/null \
    && echo "    seeded" \
    || { echo "    FAILED — aborting dispatch"; return 1; }
  echo "==> Dispatching workflow"
  gh workflow run dispatch-sow.yml \
    --repo Prog-Strength/prog-strength-developer \
    --field sow_path="$sow_path" \
    && echo "    dispatched. Watch: https://github.com/Prog-Strength/prog-strength-developer/actions"
}
```

Then:

```bash
dispatch-sow sows/whatever.md
```

The re-seed and dispatch run back-to-back with no chance for local
Claude to rotate tokens in between.

### Long-term hardening options

If the per-dispatch re-seed becomes friction, the next steps in
ascending cost are:

1. **Dedicated Claude Code Max account for the worker.** Sign up on a
   separate email, log in on a device you don't touch day-to-day,
   extract once. The worker's account never rotates because nothing
   else uses it. Cost: ~$200/month for a second Max subscription.

2. **Switch the worker to `ANTHROPIC_API_KEY`.** Add the env var to
   the worker's userdata and unset the Keychain extraction step. No
   rotation problem because there's no refresh token to invalidate.
   Bills per-token directly to your Anthropic API account, bypassing
   Max. Likely the right call only if you outgrow Max-rate limits.

3. **Service-account OAuth from Anthropic** (wishful — does not exist
   as of 2026-06-02). If Anthropic exposes a long-lived service token
   in the future, switch the worker to that.

## Watching a worker live

Every dispatch's GitHub Actions Summary now includes the exact
`aws logs tail` command for that worker. Copy-paste it. The three
streams CloudWatch will have for an in-flight worker are:

| Stream                                       | What's in it                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------------- |
| `<sow-slug>/<instance-id>/claude`            | Claude's live `--verbose` output — tool calls, file reads/writes, subagents.  |
| `<sow-slug>/<instance-id>/userdata`          | Bootstrap progress: package installs, secret fetches, repo clones, etc.       |
| `<sow-slug>/<instance-id>/cloud-init`        | cloud-init's own log (the OS-level wrapper around userdata).                  |

Most of the time you want the `/claude` stream:

```bash
aws logs tail /aws/ec2/prog-strength-developer \
  --log-stream-names <sow-slug>/<instance-id>/claude \
  --follow
```

`--follow` streams new lines as they arrive (lag is typically <5s once the
CloudWatch agent is up). Drop `--follow` for a one-shot read of the last hour.

If you don't know the instance ID, find it from the Actions Summary or:

```bash
aws ec2 describe-instances \
  --filters 'Name=tag:Name,Values=prog-strength-developer-worker' \
            'Name=instance-state-name,Values=pending,running' \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`SOW`]|[0].Value,LaunchTime]' \
  --output table
```

## SSM into a running worker

When CloudWatch is silent or you want to inspect filesystem state directly,
shell into the worker via SSM:

```bash
aws ssm start-session --target <instance-id>
```

Once in the session:

```bash
sudo -i

# Live-tail the same logs that ship to CloudWatch (bypass any agent
# shipping delay or buffering).
tail -f /var/log/prog-strength-developer/claude.log
tail -f /var/log/prog-strength-developer/userdata.log

# Is claude actually running?
ps -ef | grep -E '[c]laude|[s]cript'

# Is the CloudWatch agent healthy? (If not, that's why CloudWatch is empty.)
systemctl status amazon-cloudwatch-agent
journalctl -u amazon-cloudwatch-agent --no-pager -n 50

# What does the workspace look like? (cloned repos, branches, prompt.md)
ls /workspace
sudo -u developer git -C /workspace/<repo> status
sudo -u developer git -C /workspace/<repo> log --oneline -5
```

If Claude appears wedged (no progress in claude.log for several minutes,
no CPU on `claude` in `top`), the cleanest recovery is to terminate the
instance — Claude's partial PRs are still in GitHub if it got that far.

```bash
# From your laptop, NOT inside the SSM session:
aws ec2 terminate-instances --instance-ids <instance-id>
```

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
   If one's still alive, either the work is in progress (use the live-tail
   command above to confirm) or the backstop hasn't fired yet.

2. **Read CloudWatch logs.** From the GitHub Actions Summary, grab the
   `<sow-slug>/<instance-id>/claude` stream name and:
   ```bash
   aws logs tail /aws/ec2/prog-strength-developer \
     --log-stream-names <sow-slug>/<instance-id>/claude \
     --since 1h
   ```
   Scan the last ~200 lines. If `/claude` is empty but `/userdata` has
   content, bootstrap failed before reaching Claude — check
   `<sow-slug>/<instance-id>/userdata` for the error.

3. **SSM into the box** if it's still alive (see the section above).

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

### "Failed to authenticate. API Error: 401 Invalid authentication credentials"

The Claude OAuth tokens in Secrets Manager have been rotated out by
your local Claude Code usage. See the "Dispatching a SOW" section at
the top of this file for the explanation and the `dispatch-sow` shell
function that prevents this from happening.

If you dispatched via the GitHub UI (or otherwise without re-seeding
first), just re-extract and re-dispatch using `dispatch-sow`. The
existing in-flight worker has already self-terminated.

If `dispatch-sow` itself fails immediately — meaning the re-seed
step worked but the worker STILL gets 401 — then either:

- The Keychain entry name on your machine isn't `Claude Code-credentials`.
  Run `security dump-keychain | grep -i claude` to find the right
  service name, then update the `dispatch-sow` function.
- The OAuth tokens have been revoked entirely (you logged out, or
  Anthropic revoked them for some reason). Run `claude login`
  locally to mint a fresh set, then dispatch again.

### "PRs opened but worker still running"

Either Claude wrote PRs but didn't exit cleanly (the 6h backstop will
eventually fire), or the script is stuck on a downstream step. SSM in
(see "SSM into a running worker" above) and check `ps -ef | grep claude`
and `tail -f /var/log/prog-strength-developer/claude.log`.

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

## "CloudWatch streams are completely empty for an instance that ran"

The CloudWatch agent is installed and configured partway through the
userdata script (after the dependency-install phase). Failures during
those early steps (e.g., a `dnf install` glitch) terminate the instance
before the agent ever ships a byte to CloudWatch, so all three streams
(`/userdata`, `/claude`, `/cloud-init`) stay empty.

Two recourses:

1. **If the instance is still alive** (rare — the ERR trap usually
   terminates it within seconds): SSM in (see "SSM into a running worker"
   above) and read `/var/log/cloud-init-output.log` and
   `/var/log/prog-strength-developer/userdata.log` directly. The userdata
   writes everything to those local files via the `exec > >(tee)` redirect,
   so failure context is captured even when nothing reaches CloudWatch.
2. **If the instance is gone:** there's no remote log to read. Re-dispatch
   with the same SOW and watch CloudWatch live (the agent will start
   shipping bytes once it gets past `systemctl enable --now amazon-cloudwatch-agent`).
   If the failure is reproducible at the same point, that's the diagnostic
   evidence.

A future hardening pass could pre-install + start the CloudWatch agent
via a custom AMI, closing this gap entirely.

## "/claude stream is empty but /userdata is fine"

Three possibilities, in order of likelihood:

1. **Bootstrap failed before reaching Claude.** Read the tail of
   `/userdata` — the last `log` line will tell you where it died.
2. **Claude started but produced no output yet.** Now that claude runs
   under `script -qfc` with `--verbose`, you should see progress within
   seconds of "Starting Claude Code". If you don't, SSM in and check
   `ps -ef | grep -E '[c]laude|[s]cript'` — if claude is running but
   silent, that's an upstream issue (network to api.anthropic.com,
   credentials, etc.).
3. **CloudWatch agent failed to pick up the file.** SSM in and run
   `journalctl -u amazon-cloudwatch-agent --no-pager -n 100` — look for
   "file not found" or permission errors against
   `/var/log/prog-strength-developer/claude.log`.

## "How do I see what Claude was thinking on a failed run?"

CloudWatch retains 30 days of logs. Open the AWS Console → CloudWatch
Logs → `/aws/ec2/prog-strength-developer`. Streams are named
`<sow-slug>/<instance-id>/<source>`; the console renders the `/` as a
folder hierarchy so all streams for one worker group together under
the SOW.

Three streams per worker:

- `.../<instance-id>/claude` — Claude's `--verbose` output: tool calls,
  reasoning, subagent dispatches, file reads/writes. **This is where
  failures usually show up.**
- `.../<instance-id>/userdata` — `[userdata ...]` progress markers from
  the bootstrap script. Useful for bootstrap-phase failures.
- `.../<instance-id>/cloud-init` — cloud-init's own log, useful when
  the box itself didn't boot cleanly.

The last 50–100 lines of `/claude` are usually where the failure mode
is most diagnostic.
