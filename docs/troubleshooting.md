# Troubleshooting

## Table of contents

- [Dispatching a SOW (read before every dispatch)](#dispatching-a-sow-important--read-before-each-dispatch)
- [Watching a worker live](#watching-a-worker-live)
- [SSM into a running worker](#ssm-into-a-running-worker)
- ["Worker dispatched but no PRs ever appeared"](#worker-dispatched-but-no-prs-ever-appeared)
- [Common failure modes](#common-failure-modes)
  - [Bootstrap dies fetching secrets](#bootstrap-dies-fetching-secrets)
  - [GitHub App token mint fails with 401](#github-app-token-mint-fails-with-401)
  - [Claude auth 401: invalid authentication credentials](#failed-to-authenticate-api-error-401-invalid-authentication-credentials)
  - [PRs opened but worker still running](#prs-opened-but-worker-still-running)
  - [Workflow fails on preflight check](#workflow-fails-on-preflight-check)
  - [Terraform: OIDC provider does not exist](#terraform-apply-fails-with-oidc-provider-does-not-exist)
  - [Terraform: launch template recreated every run](#terraform-plan-wants-to-destroy-and-recreate-the-launch-template-every-run)
- ["I want to nuke everything and start over"](#i-want-to-nuke-everything-and-start-over)
- ["CloudWatch streams are completely empty for an instance that ran"](#cloudwatch-streams-are-completely-empty-for-an-instance-that-ran)
- ["/claude stream is empty but /userdata is fine"](#claude-stream-is-empty-but-userdata-is-fine)
- ["How do I see what Claude was thinking on a failed run?"](#how-do-i-see-what-claude-was-thinking-on-a-failed-run)

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

| Stream                                       | What's in it                                                                                                                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `<sow-slug>/<instance-id>/claude`            | Rendered claude code session events — one line per `tool-use` / `tool-result` / `assistant` turn, decoded by the renderer sidecar from claude's native JSONL transcript.  |
| `<sow-slug>/<instance-id>/userdata`          | Bootstrap progress: package installs, secret fetches, repo clones, etc.                                                                                                   |
| `<sow-slug>/<instance-id>/cloud-init`        | cloud-init's own log (the OS-level wrapper around userdata).                                                                                                              |

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
tail -f /var/log/prog-strength-developer/claude-pretty.log    # rendered events stream (== "claude" CW stream)
tail -f /var/log/prog-strength-developer/userdata.log         # bootstrap progress

# Raw JSONL transcript — machine-truth, every assistant turn / tool
# use / tool result with full content. The renderer sidecar tails
# these and produces claude-pretty.log; read the source directly
# when you suspect the renderer is at fault or need un-truncated
# tool output.
sudo tail -f /home/developer/.claude/projects/*/*.jsonl

# Is claude actually running?
ps -ef | grep -E '[c]laude|[j]q|[t]ail.*projects'

# Is the CloudWatch agent healthy? (If not, that's why CloudWatch is empty.)
systemctl status amazon-cloudwatch-agent
journalctl -u amazon-cloudwatch-agent --no-pager -n 50

# What does the workspace look like? (cloned repos, branches, prompt.md)
ls /workspace
sudo -u developer git -C /workspace/<repo> status
sudo -u developer git -C /workspace/<repo> log --oneline -5
```

If Claude appears wedged (no progress in claude-pretty.log for several
minutes, no CPU on `claude` in `top`), the cleanest recovery is to
terminate the instance — Claude's partial PRs are still in GitHub if
it got that far.

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
and `tail -f /var/log/prog-strength-developer/claude-pretty.log`.

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

The `/claude` stream is **not** populated by claude's stdout — claude
code 2.1.161 emits nothing to stdout in `--print` mode. It writes
structured events to `~/.claude/projects/<slug>/<uuid>.jsonl`, and a
renderer sidecar in userdata.sh.tpl tails those files, decodes each
event into one human-readable line, and appends to
`/var/log/prog-strength-developer/claude-pretty.log`, which the
CloudWatch agent ships as the `claude` stream.

Three possibilities for an empty `/claude` stream, in order of likelihood:

1. **Bootstrap failed before reaching Claude.** Read the tail of
   `/userdata` — the last `log` line will tell you where it died.
2. **The renderer sidecar isn't running, or the source JSONL hasn't
   appeared yet.** SSM in and check:
   ```bash
   # Is the sidecar pipeline alive? (look for the jq + tail processes)
   ps -ef | grep -E '[j]q|[t]ail.*projects'
   cat /run/claude-pretty-renderer.pid 2>/dev/null

   # Is the source JSONL being written?
   sudo ls -la /home/developer/.claude/projects/*/

   # Is the rendered file growing?
   ls -la /var/log/prog-strength-developer/claude-pretty.log
   ```
   If the JSONL is growing but `claude-pretty.log` isn't, jq is stuck
   or died — kill the pid in `/run/claude-pretty-renderer.pid` and
   re-run the sidecar block by hand, or terminate the instance and
   redispatch.
3. **CloudWatch agent failed to pick up the file.** SSM in and run
   `journalctl -u amazon-cloudwatch-agent --no-pager -n 100` — look for
   "file not found" or permission errors against
   `/var/log/prog-strength-developer/claude-pretty.log`.

**Historical note (pre-renderer-sidecar workers):** Older launch
template versions ran claude under `script -qfc` with `--verbose`
expecting verbose output to stream to stdout. It never did — that's
why this stream was empty for so long. If you find a worker still
using `script -qfc` in its userdata, terraform apply hasn't picked
up the renderer-based version yet.

## "How do I see what Claude was thinking on a failed run?"

CloudWatch retains 30 days of logs. Open the AWS Console → CloudWatch
Logs → `/aws/ec2/prog-strength-developer`. Streams are named
`<sow-slug>/<instance-id>/<source>`; the console renders the `/` as a
folder hierarchy so all streams for one worker group together under
the SOW.

Three streams per worker:

- `.../<instance-id>/claude` — rendered session events: `tool-use`,
  `tool-result`, `assistant`, one per JSON event from the renderer
  sidecar. **This is where failures usually show up.**
- `.../<instance-id>/userdata` — `[userdata ...]` progress markers from
  the bootstrap script. Useful for bootstrap-phase failures.
- `.../<instance-id>/cloud-init` — cloud-init's own log, useful when
  the box itself didn't boot cleanly.

The last 50–100 lines of `/claude` are usually where the failure mode
is most diagnostic. For deeper inspection (full prompts, un-truncated
tool input/output, subagent sidechain calls), SSM into a still-running
worker and read the source JSONL directly at
`/home/developer/.claude/projects/*/*.jsonl` — the rendered stream
truncates tool-result content at 300 chars; the source has everything.

## "No workers showing on the Grafana fleet panel"

The Developer Platform dashboard's "Active workers" stat reads zero
when no `developer_worker_info` series are alive in Prometheus. Walk
the layers in order:

1. **Is a worker actually running?**
   `aws ec2 describe-instances --filters
   Name=tag:Name,Values=prog-strength-developer-worker
   Name=instance-state-name,Values=running`.
2. **Is Prometheus discovering it?** SSH-tunnel or SSM-port-forward to
   `developers.progstrength.fitness/d/manager-host-health` (or use the
   `/d/manager-host-health` URL). Open Prometheus → Status → Targets.
   The `developer_worker_node` and `developer_worker_exporter` jobs
   should list the worker's private IP. If they don't, the manager's
   IAM role may be missing `ec2:DescribeInstances` (see `manager.tf`).
3. **Are the exporters up on the worker?** SSM into the worker:
   `systemctl status worker_exporter node_exporter`. If either is
   inactive, `journalctl -u worker_exporter -e` is the next step.
4. **Is the SG path open?** From the manager:
   `curl http://<worker-private-ip>:9101/metrics`. A timeout means
   the manager → worker ingress rule on port 9101 (see
   `manager.tf`'s `worker_ingress_worker_exporter`) was lost or
   never applied.

## "Pushgateway target down in Prometheus"

The `pushgateway` job on Prometheus → Status → Targets is unreachable.

1. **Is the container running?** SSM into the manager and run
   `docker compose -f /opt/prog-strength-developer/monitoring/docker-compose.yml ps`.
   `docker compose up -d pushgateway` from the same directory restarts
   it if needed.
2. **Is the persistence file readable?** `docker compose logs
   pushgateway`. A corrupt `/var/lib/manager/pushgateway/pushgateway.dat`
   after an unclean stop manifests as Pushgateway exiting on boot.
   The file is not authoritative — workers re-push on every termination
   — so deleting it and restarting is safe.
3. **Did the most recent worker actually push?** `journalctl -u
   cloud-init-output.service -e` on a recently-terminated worker (via
   the CloudWatch `userdata` stream) shows the `finalize_metrics:
   pushed` line on success or `push to <ip>:9091 failed` on failure.

## "Live Claude output panel shows 'no data'" (stretch goal)

Loki is the stretch-goal log-tail. If the panel is dead:

1. **Is Promtail running on the worker?** SSM in and
   `systemctl status promtail`. If it never started, the
   `manager_private_ip` substitution at userdata render time was
   probably empty — verify the dispatch workflow's `Resolve persistent
   infra IDs` step found a manager.
2. **Is Loki up?** SSM the manager and `docker compose ps`. Loki at
   ~200-300MB resident is the most likely candidate to be evicted if
   the manager runs out of memory; the Manager Host Health "Per-
   container memory" panel shows it.
3. **Did Promtail reach Loki?** From the worker:
   `curl -fsS http://<manager-private-ip>:3100/ready`. 200 = Loki
   alive and listening. Connection refused = the manager SG's
   port-3100 ingress from worker SG (see `manager.tf`'s
   `manager_ingress_loki`) was lost.
