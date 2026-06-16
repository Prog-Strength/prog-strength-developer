#!/bin/bash
# prog-strength-developer worker userdata.
# Rendered by Terraform's templatefile() — variables wrapped in
# $${...} are substituted at apply time.
#
# Lifecycle: install deps → fetch secrets → clone repos → run Claude
# Code → self-terminate. Every branch terminates the instance,
# including failures. A systemd timer is set as a hard backstop.

set -euo pipefail

# cloud-init runs userdata as root WITHOUT $HOME set, which combined
# with set -u (above) kills the script the first time anything reads
# $HOME — uv's PATH export was the culprit on the first real run.
# Set HOME explicitly so every subsequent installer (uv, npm, claude,
# etc.) writes its dotfiles to /root/ as expected.
export HOME=/root

LOG_DIR=/var/log/prog-strength-developer
mkdir -p "$LOG_DIR"
WORKDIR=/workspace
mkdir -p "$WORKDIR"

# Tee all subsequent output so the CloudWatch agent picks it up.
exec > >(tee -a "$LOG_DIR/userdata.log") 2>&1

log() { echo "[userdata $(date -u +%FT%TZ)] $*"; }

# Defined BEFORE the ERR trap so failures during the early IMDS fetch
# (or any other line above the trap's first chance to fire) still reach
# a real function rather than a "command not found" no-op. INSTANCE_ID
# is fetched lazily inside the function so it works even if the
# top-of-script IMDS call hasn't succeeded yet.
finalize_metrics() {
  # Push end-of-run summary to the manager's Pushgateway so completed-run
  # facts survive worker termination. Best-effort: a failed push must
  # not block termination — the worker dies either way.
  local outcome="$${1:-error}"
  local mgr="${manager_private_ip}"
  if [ -z "$mgr" ]; then
    log "finalize_metrics: no manager_private_ip; skipping push"
    return 0
  fi
  local finished_at
  finished_at=$(date +%s)
  local started_at_label="$${STARTED_AT:-$finished_at}"
  local duration=$(( finished_at - started_at_label ))
  local prs_count
  prs_count=$(cat /var/run/developer-worker/prs_opened 2>/dev/null || echo 0)
  local sow_label="${sow_path}"
  # Pushgateway expects bare text exposition. Labels embedded in the
  # job/instance URL path become target labels on Prometheus's side
  # because the scrape uses honor_labels. started_at is encoded as a
  # label (not the metric value) so the Run history dashboard panel
  # can render each row's boot timestamp and sort by recency; the
  # companion _finished_at_seconds metric carries the epoch as a value
  # so "Completed runs (24h)" can filter by (time() - finished_at).
  local payload
  payload=$(cat <<EOF
# TYPE developer_run_duration_seconds gauge
developer_run_duration_seconds{sow="$sow_label",outcome="$outcome",started_at="$started_at_label"} $duration
# TYPE developer_run_prs_opened gauge
developer_run_prs_opened{sow="$sow_label",outcome="$outcome",started_at="$started_at_label"} $prs_count
# TYPE developer_run_finished_at_seconds gauge
developer_run_finished_at_seconds{sow="$sow_label",outcome="$outcome",started_at="$started_at_label"} $finished_at
EOF
)
  # Pushgateway parses the body as Prometheus text-exposition format,
  # which REQUIRES the final metric line to end in a newline. The payload
  # is built with $(cat <<EOF...), and command substitution strips every
  # trailing newline — so sending "$payload" directly gives the gateway a
  # body with no terminating newline and it rejects the WHOLE push with
  # HTTP 400 ("unexpected end of input stream"). curl -fsS then fails
  # silently and the run is never recorded, which is why the dashboard's
  # run-history / completed / failure panels stayed empty. Pipe through
  # printf '%s\n' to re-add the trailing newline and stream it from stdin.
  if printf '%s\n' "$payload" | curl -fsS --max-time 10 \
       -X POST --data-binary @- \
       "http://$mgr:9091/metrics/job/developer_run/instance/$INSTANCE_ID" \
       >/dev/null 2>&1; then
    log "finalize_metrics: pushed (outcome=$outcome, started_at=$started_at_label, duration=$${duration}s, prs=$prs_count)"
  else
    log "finalize_metrics: push to $mgr:9091 failed (continuing)"
  fi
  # Brief sleep so Prometheus's next 15s scrape of Pushgateway lands
  # the new sample before the worker's series goes stale.
  sleep 2
}

# Release this worker's SOW lock in the fleet run registry so the SOW can
# be dispatched again. Best-effort by design: if the fleet package isn't
# on disk yet (a failure before the repo clone) or the call errors, the
# lock's expires_at TTL reclaims the SOW. This is the prompt path; the
# TTL is the correctness backstop. Every command here is guarded so the
# ERR trap can call it without re-entering.
release_sow_lock() {
  local outcome="$${1:-error}"
  local repo=/opt/prog-strength-developer-repo
  if [ ! -d "$repo/fleet" ]; then
    log "fleet package not present; skipping SOW lock release (TTL will reclaim)"
    return 0
  fi
  log "Releasing SOW lock for ${sow_path} (outcome=$outcome)"
  AWS_REGION="${aws_region}" PYTHONPATH="$repo" python3 -m fleet release \
    --sow "${sow_path}" \
    --instance-id "$${INSTANCE_ID:-none}" \
    --outcome "$outcome" || log "fleet release errored (TTL will reclaim the lock)"
}

terminate_self() {
  local outcome="$${1:-error}"
  echo terminating > /var/run/developer-worker/state 2>/dev/null || true
  finalize_metrics "$outcome"
  release_sow_lock "$outcome"
  local iid="$${INSTANCE_ID:-}"
  if [ -z "$iid" ]; then
    local token
    token=$(curl -fsS -X PUT \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
      http://169.254.169.254/latest/api/token 2>/dev/null || true)
    if [ -n "$token" ]; then
      iid=$(curl -fsS -H "X-aws-ec2-metadata-token: $token" \
        http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || true)
    fi
  fi
  log "Terminating instance $iid"
  sleep 10
  if [ -n "$iid" ]; then
    aws ec2 terminate-instances \
      --region "${aws_region}" \
      --instance-ids "$iid" || true
  fi
}

trap 'log "FATAL: userdata exited at line $LINENO with status $?"; terminate_self error' ERR

INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token)" \
  http://169.254.169.254/latest/meta-data/instance-id)
export INSTANCE_ID

# Boot timestamp used by the Pushgateway finalize push to compute the
# run's duration. Captured ASAP so a failure in any later stage still
# attributes a real duration to the run.
STARTED_AT=$(date +%s)
export STARTED_AT

# State file consumed by worker_exporter and updated at each lifecycle
# transition below.
mkdir -p /var/run/developer-worker
echo booting > /var/run/developer-worker/state
echo 0 > /var/run/developer-worker/prs_opened

# Derive a CloudWatch-stream-safe SOW slug from sow_path early so the
# CW agent config below can name streams after the SOW. basename strips
# the directory and .md extension; the tr+sed pipeline coerces anything
# outside [A-Za-z0-9._-] to '_' and trims trailing underscores (the
# trailing newline from basename becomes '_' under tr -c, hence the
# trim). Done up front so a single value is reused everywhere.
SOW_SLUG=$(basename "${sow_path}" .md | tr -c 'A-Za-z0-9._-' '_' | sed 's/_*$//')
STREAM_PREFIX="$SOW_SLUG/$INSTANCE_ID"
export SOW_SLUG STREAM_PREFIX

# --------------------------------------------------------------------
# Install dependencies.
# --------------------------------------------------------------------
log "Installing system packages"
dnf install -y -q \
  git gcc make jq python3 python3-pip \
  amazon-cloudwatch-agent

# AWS CLI v2 is pre-installed on Amazon Linux 2023; just verify.
aws --version

# Install pyjwt (for GitHub App JWT minting) and PyYAML (for SOW
# frontmatter parsing later in the script). Both via pip on the system
# python3 — no venv needed; this is a single-purpose ephemeral host.
pip3 install --quiet 'pyjwt[crypto]>=2.8' 'pyyaml>=6'

# Install GitHub CLI.
log "Installing gh CLI"
dnf install -y -q dnf-plugins-core
dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
dnf install -y -q gh

# Install Node 20 + npm via NodeSource.
log "Installing Node 20"
curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -
dnf install -y -q nodejs

# Install Go (latest stable).
log "Installing Go"
GO_VERSION=1.22.5
curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go$${GO_VERSION}.linux-amd64.tar.gz"
tar -C /usr/local -xzf /tmp/go.tgz
export PATH="$PATH:/usr/local/go/bin"
echo 'export PATH="$PATH:/usr/local/go/bin"' >> /etc/profile.d/go.sh

# Install the pre-commit gate tooling the cloned repos rely on. The Go
# repos' pre-push hook shells out to `golangci-lint` and `pre-commit`
# from PATH (language: system hooks), so both must be on the developer
# user's PATH. golangci-lint is pinned to prog-strength-api's CI release
# (v2.12.2) so the local gate and CI agree exactly — a different version
# can pass locally yet fail CI (or vice versa). Installed to
# /usr/local/bin, which is on every login shell's PATH.
log "Installing pre-commit gate tooling (pre-commit, golangci-lint v2.12.2)"
pip3 install --quiet 'pre-commit>=3'
curl -fsSL https://raw.githubusercontent.com/golangci/golangci-lint/v2.12.2/install.sh \
  | sh -s -- -b /usr/local/bin v2.12.2

# Install uv (Python project + tool manager).
log "Installing uv"
curl -fsSL https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:/root/.local/bin:$PATH"

# Install Claude Code CLI via the official npm package.
log "Installing Claude Code"
npm install -g --silent @anthropic-ai/claude-code

# --------------------------------------------------------------------
# Install node_exporter (host metrics) on :9100 and stub out the
# worker_exporter systemd unit on :9101. Prometheus on the manager
# (10.20.2.x) scrapes both via ec2_sd_config using this instance's
# private IP. The exporter script itself is dropped in further below,
# after the prog-strength-developer repo has been cloned.
# --------------------------------------------------------------------
log "Installing node_exporter"
NE_VERSION=1.9.1
curl -fsSL -o /tmp/ne.tgz \
  "https://github.com/prometheus/node_exporter/releases/download/v$${NE_VERSION}/node_exporter-$${NE_VERSION}.linux-amd64.tar.gz"
tar -C /tmp -xzf /tmp/ne.tgz
install -m 0755 "/tmp/node_exporter-$${NE_VERSION}.linux-amd64/node_exporter" /usr/local/bin/node_exporter
cat > /etc/systemd/system/node_exporter.service <<'EOF'
[Unit]
Description=Prometheus node_exporter
After=network.target

[Service]
ExecStart=/usr/local/bin/node_exporter --web.listen-address=:9100
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now node_exporter

log "Installing worker_exporter + fleet dependencies"
# prometheus_client: worker_exporter metrics. boto3: the `fleet` package's
# SOW-lock release at finalize (python3 -m fleet release).
pip3 install --quiet 'prometheus_client>=0.20' 'boto3>=1.34'

# --------------------------------------------------------------------
# Promtail (stretch goal): tail claude-pretty.log and ship to Loki on
# the manager so the Grafana "Live Claude output" panel works. Only
# starts if manager_private_ip is populated — keeps the stretch goal
# truly optional.
# --------------------------------------------------------------------
log "Installing Promtail"
PROMTAIL_VERSION=3.2.0
curl -fsSL -o /tmp/promtail.zip \
  "https://github.com/grafana/loki/releases/download/v$${PROMTAIL_VERSION}/promtail-linux-amd64.zip"
unzip -q /tmp/promtail.zip -d /tmp
install -m 0755 /tmp/promtail-linux-amd64 /usr/local/bin/promtail
mkdir -p /etc/promtail /var/lib/promtail
cat > /etc/promtail/config.yml <<EOF
server:
  http_listen_port: 9080
  grpc_listen_port: 0

positions:
  filename: /var/lib/promtail/positions.yaml

clients:
  - url: http://${manager_private_ip}:3100/loki/api/v1/push

scrape_configs:
  - job_name: claude
    static_configs:
      - targets: [localhost]
        labels:
          job: claude
          instance_id: "$INSTANCE_ID"
          sow: "${sow_path}"
          __path__: /var/log/prog-strength-developer/claude-pretty.log
EOF
cat > /etc/systemd/system/promtail.service <<'EOF'
[Unit]
Description=Promtail (Loki shipper)
After=network.target

[Service]
ExecStart=/usr/local/bin/promtail -config.file=/etc/promtail/config.yml
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
if [ -n "${manager_private_ip}" ]; then
  systemctl enable --now promtail
else
  log "Promtail not started — manager_private_ip empty (stretch goal off)"
fi
# Unquoted heredoc so $INSTANCE_ID, $STARTED_AT, and ${sow_path} expand
# at write time. The exporter script itself is installed after the
# prog-strength-developer repo clone further down.
cat > /etc/systemd/system/worker_exporter.service <<EOF
[Unit]
Description=prog-strength-developer worker exporter
After=network.target

[Service]
Environment=SOW_PATH=${sow_path}
Environment=INSTANCE_ID=$INSTANCE_ID
Environment=STARTED_AT=$STARTED_AT
ExecStart=/usr/bin/python3 /usr/local/bin/worker_exporter.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# --------------------------------------------------------------------
# Configure CloudWatch agent to ship logs to the developer log group
# under predictable per-source streams: <sow-slug>/<instance-id>/<source>.
# The console renders the '/' separators as a folder hierarchy, and
# splitting userdata vs claude vs cloud-init lets operators tail just
# Claude's progress without the bootstrap noise.
# --------------------------------------------------------------------
log "Configuring CloudWatch agent (streams under $STREAM_PREFIX/)"
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "$LOG_DIR/userdata.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$STREAM_PREFIX/userdata",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "retention_in_days": -1
          },
          {
            "file_path": "$LOG_DIR/claude-pretty.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$STREAM_PREFIX/claude",
            "retention_in_days": -1
          },
          {
            "file_path": "$LOG_DIR/claude.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$STREAM_PREFIX/claude-debug",
            "retention_in_days": -1
          },
          {
            "file_path": "/var/log/cloud-init-output.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$STREAM_PREFIX/cloud-init",
            "retention_in_days": -1
          }
        ]
      }
    }
  }
}
EOF
systemctl enable --now amazon-cloudwatch-agent
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config \
  -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
  -s

# --------------------------------------------------------------------
# Create the non-root user that will run Claude Code.
#
# `claude --dangerously-skip-permissions` refuses to run as root for
# security reasons, and cloud-init runs userdata as root. Everything
# above (package installs, CW agent, etc.) needed root, but the work
# below (clones, gh ops, claude itself) can run unprivileged. We
# create the user here so it exists before the secrets/auth setup
# below, which writes credentials into both /root/ AND /home/developer/.
# --------------------------------------------------------------------
log "Creating non-root developer user (claude refuses to run as root)"
useradd -m -s /bin/bash developer
DEV_HOME=/home/developer

# --------------------------------------------------------------------
# Fetch secrets.
# --------------------------------------------------------------------
log "Fetching Claude credentials from Secrets Manager"
# Claude Code on Linux reads ~/.claude/.credentials.json (leading dot
# on the filename — it's a hidden file inside the already-hidden
# .claude dir). On macOS the same JSON blob lives in Keychain; the
# user extracts it via `security find-generic-password ... -w` when
# seeding the secret. Either way the contents are the same.
mkdir -p /root/.claude
aws secretsmanager get-secret-value \
  --region "${aws_region}" \
  --secret-id "${claude_secret_name}" \
  --query SecretString --output text > /root/.claude/.credentials.json
chmod 600 /root/.claude/.credentials.json

# Also copy to the developer user's home so claude can authenticate
# when invoked as developer below.
mkdir -p "$DEV_HOME/.claude"
cp /root/.claude/.credentials.json "$DEV_HOME/.claude/.credentials.json"
chmod 600 "$DEV_HOME/.claude/.credentials.json"
chown -R developer:developer "$DEV_HOME/.claude"

log "Fetching GitHub App credentials"
aws secretsmanager get-secret-value \
  --region "${aws_region}" \
  --secret-id "${github_app_secret_name}" \
  --query SecretString --output text > /root/.github-app.json
chmod 600 /root/.github-app.json

# Extract the JWT-minting Python to /root/mint-gh-token.py so it's
# reusable from both the initial login and the periodic re-mint loop
# below. Installation tokens expire in 1 hour; SOWs commonly run
# longer than that, so we re-mint every 50 minutes to keep `gh push`
# and `gh pr create` working through the whole run.
log "Writing GitHub App token minter to /root/mint-gh-token.py"
cat > /root/mint-gh-token.py <<'PY'
import json, time, urllib.request, sys
import jwt

with open('/root/.github-app.json') as f:
    cfg = json.load(f)

now = int(time.time())
payload = {
    "iat": now - 60,
    "exp": now + 540,
    "iss": str(cfg["app_id"]),
}
encoded = jwt.encode(payload, cfg["private_key"], algorithm="RS256")

req = urllib.request.Request(
    f"https://api.github.com/app/installations/{cfg['installation_id']}/access_tokens",
    method="POST",
    headers={
        "Authorization": f"Bearer {encoded}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
)
with urllib.request.urlopen(req) as resp:
    body = json.loads(resp.read())
print(body["token"])
PY
chmod 600 /root/mint-gh-token.py

log "Minting initial GitHub App installation token"
GH_TOKEN=$(python3 /root/mint-gh-token.py)
# `<<<` here-string keeps the token off any pipe that could be teed
# elsewhere. The token still transits the process boundary to gh's
# stdin only.
#
# Authenticate gh as BOTH root (for the script's own clones below) and
# developer (so claude's gh subprocesses below can push branches and
# open PRs). Each user has its own gh config dir; tokens are stored
# separately under /root/.config/gh and /home/developer/.config/gh.
gh auth login --with-token <<< "$GH_TOKEN"
sudo -u developer gh auth login --with-token <<< "$GH_TOKEN"
unset GH_TOKEN

# Background re-mint loop. Runs every 50 minutes (token TTL is 60 min).
# Refreshes both root's and developer's gh auth so neither hits a 401
# during long-running SOWs. Failures inside the loop log a warning
# rather than killing the run; at worst gh's next operation 401s and
# Claude retries.
(
  while true; do
    sleep 3000
    if NEW_TOKEN=$(python3 /root/mint-gh-token.py 2>/dev/null); then
      gh auth login --with-token <<< "$NEW_TOKEN" >/dev/null 2>&1 || true
      sudo -u developer gh auth login --with-token <<< "$NEW_TOKEN" >/dev/null 2>&1 \
        && echo "[token-refresh $(date -u +%FT%TZ)] re-minted GitHub App token (root + developer)" \
        || echo "[token-refresh $(date -u +%FT%TZ)] WARNING: re-login failed for one or both users"
      unset NEW_TOKEN
    else
      echo "[token-refresh $(date -u +%FT%TZ)] WARNING: GH token re-mint failed; continuing with stale tokens"
    fi
  done
) &
echo $! > /run/gh-token-refresh.pid

# --------------------------------------------------------------------
# Set the 6h hard backstop. systemd-run --on-active is the simplest
# transient unit for "fire once after N hours."
# --------------------------------------------------------------------
log "Arming ${max_runtime_hours}h backstop"
systemd-run \
  --on-active="${max_runtime_hours}h" \
  --unit=worker-timeout \
  /bin/bash -c "aws ec2 terminate-instances --region ${aws_region} --instance-ids $INSTANCE_ID"

# --------------------------------------------------------------------
# Clone prog-strength-docs, parse the SOW frontmatter for the repo
# list, clone each affected repo.
# --------------------------------------------------------------------
echo cloning > /var/run/developer-worker/state
log "Cloning prog-strength-docs"
cd "$WORKDIR"
gh repo clone "${github_org}/prog-strength-docs"

SOW_FILE="$WORKDIR/prog-strength-docs/${sow_path}"
if [ ! -f "$SOW_FILE" ]; then
  log "FATAL: SOW not found at $SOW_FILE"
  terminate_self
  exit 1
fi

log "Reading repo list from SOW frontmatter"
# Extract the YAML frontmatter block (between the first pair of '---')
# and pull repos[] via python (always available on AL2023 + pip).
REPOS=$(python3 - <<PY
import re, sys, yaml
with open("$SOW_FILE") as f:
    text = f.read()
m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
if not m:
    print("FATAL: SOW has no YAML frontmatter", file=sys.stderr)
    sys.exit(1)
meta = yaml.safe_load(m.group(1)) or {}
for r in meta.get("repos") or []:
    print(r)
PY
)

if [ -z "$REPOS" ]; then
  log "FATAL: SOW frontmatter has empty repos:[]"
  terminate_self
  exit 1
fi

log "Repos to clone: $REPOS"
for repo in $REPOS; do
  if [ "$repo" = "prog-strength-docs" ]; then
    continue   # already cloned
  fi
  log "Cloning $repo"
  gh repo clone "${github_org}/$repo" "$WORKDIR/$repo"
done

# --------------------------------------------------------------------
# Render the Claude prompt and run Claude Code.
#
# The prompt template lives in this repo at bootstrap/prompt.md.tpl.
# Cloning prog-strength-developer at boot (rather than inlining the
# template into userdata) keeps the rendered user_data well under
# EC2's 16KB limit and lets iteration on the prompt happen via PR
# instead of a launch-template replacement.
# --------------------------------------------------------------------
log "Cloning prog-strength-developer for prompt template"
gh repo clone "${github_org}/prog-strength-developer" /opt/prog-strength-developer-repo

# Now that the repo is on disk, drop the exporter script into place and
# start the unit registered earlier. State transitions to 'working'
# right before we hand off to Claude.
install -m 0755 /opt/prog-strength-developer-repo/bootstrap/worker_exporter.py /usr/local/bin/worker_exporter.py
systemctl enable --now worker_exporter

log "Rendering Claude prompt for SOW ${sow_path}"
# SOW_SLUG was computed at the top of the script so it could feed the
# CloudWatch agent config. Reusing the same value here keeps the prompt
# substitution and the log stream name in sync.
sed \
  -e "s|__SOW_PATH__|${sow_path}|g" \
  -e "s|__SOW_SLUG__|$SOW_SLUG|g" \
  -e "s|__GITHUB_ORG__|${github_org}|g" \
  -e "s|__TODAY__|$(date -u +%F)|g" \
  /opt/prog-strength-developer-repo/bootstrap/prompt.md.tpl \
  > "$WORKDIR/prompt.md"

# Transfer the workspace to the developer user so claude (running as
# developer) can read the prompt + cloned repos and write back the
# branches/files it produces.
chown -R developer:developer "$WORKDIR"

# Arm the pre-commit PUSH gate in every cloned repo that ships a
# pre-commit config, as the developer user. Git hooks are per-clone and
# live in each repo's .git/hooks; they are NOT cloned, so a fresh clone
# has none — which is why the worker's PRs have been reaching CI
# un-linted. Installing the pre-push hook makes the repo's pre-push
# stage (golangci-lint = lint + format, go vet, go mod tidy drift, go
# test) run on the agent's `git push`, so a branch that would fail CI
# fails locally FIRST and never becomes a red PR. We arm only pre-push
# (not the commit stage) to gate exactly what CI gates without
# per-commit formatter churn. Repos that don't ship a pre-commit config
# — husky-based repos like prog-strength-web (husky self-arms via
# `npm install`), or repos with no hook at all — are skipped by the
# conditional below and instead rely on the agent running CI's checks
# before pushing (see prompt.md.tpl, step 5). Tools are verified on the
# developer PATH first; if any is missing we skip arming rather than
# block every push with a hook that can't run.
log "Arming pre-commit push gate in cloned repos"
if sudo -i -u developer -- bash -c 'command -v pre-commit golangci-lint go >/dev/null 2>&1'; then
  for dir in "$WORKDIR"/*/; do
    if [ -f "$dir.pre-commit-config.yaml" ] || [ -f "$dir.pre-commit-config.yml" ]; then
      log "  arming pre-push hook in $dir"
      if ! sudo -i -u developer -- bash -c "cd '$dir' && pre-commit install --hook-type pre-push"; then
        log "  WARNING: pre-commit install failed in $dir — local CI gate NOT enforced for this repo"
      fi
    fi
  done
else
  log "WARNING: pre-commit/golangci-lint/go not all on developer PATH — skipping hook arming (no local CI gate)"
fi

# Stage Claude Code plugins (superpowers, frontend-design) for the
# developer user so the prompt's references to superpowers:writing-plans
# and superpowers:subagent-driven-development resolve. Best-effort: a
# failure here logs a warning but does not block the run — claude
# falls back to no-skill behavior, which is strictly worse but still
# a valid run.
log "Installing Claude Code plugins (superpowers, frontend-design)"
sudo -u developer python3 \
  /opt/prog-strength-developer-repo/bootstrap/install_plugins.py \
  /opt/prog-strength-developer-repo/bootstrap/plugins.json \
  || log "WARN: plugin install errored; claude will run without skills"

# Diagnostic: confirm developer's claude credentials are in place
# before invoking claude. A missing file here means the OAuth blob
# never reached the worker, which is a one-line failure mode worth
# surfacing distinctly from a "claude ran but auth failed" failure.
log "Developer claude credentials present:"
sudo -u developer ls -la "$DEV_HOME/.claude/" 2>&1 || true

# claude code 2.1.161 emits no stdout in --print mode; it writes
# structured session events to ~/.claude/projects/<slug>/<uuid>.jsonl.
# This sidecar tails those JSONLs and renders each event as one
# timestamped line into claude-pretty.log, which the CloudWatch agent
# ships to the "claude" stream. Without it the "claude" CW stream
# stays empty even though Claude is doing work — it's writing to a
# path the agent doesn't tail.
log "Starting JSONL-to-text renderer sidecar"
touch "$LOG_DIR/claude-pretty.log"
(
  until ls /home/developer/.claude/projects/*/*.jsonl >/dev/null 2>&1; do
    sleep 2
  done
  tail -F -q /home/developer/.claude/projects/*/*.jsonl 2>/dev/null \
    | jq --unbuffered -r '
        . as $e |
        if $e.type == "assistant" then
          ($e.message.content // []) | map(
            if .type == "text" then "[" + $e.timestamp + "] assistant: " + .text
            elif .type == "tool_use" then "[" + $e.timestamp + "] tool-use " + .name + ": " + (.input | @json)
            else empty end
          )[]
        elif $e.type == "user" then
          ($e.message.content // []) | map(
            if .type == "tool_result" then
              "[" + $e.timestamp + "] tool-result: " + (((.content | tostring) | gsub("\n"; " | "))[:300])
            else empty end
          )[]
        else empty end' \
    >> "$LOG_DIR/claude-pretty.log"
) &
echo $! > /run/claude-pretty-renderer.pid

echo working > /var/run/developer-worker/state
log "Starting Claude Code (as non-root user 'developer')"
# --print is non-interactive batch mode. --dangerously-skip-permissions
# waives interactive permission prompts; claude refuses this flag under
# root, so we drop into developer via sudo. sudo -i runs a login shell
# so $HOME is /home/developer and PATH includes the npm-global bin
# where claude installed.
#
# ANTHROPIC_LOG=debug turns on the Anthropic TS SDK's verbose fetch
# diagnostics — written to stderr, captured here into claude.log along
# with the final --print response. The CW agent ships claude.log to the
# "claude-debug" stream so SDK-level errors (e.g. "socket connection
# closed unexpectedly, pass verbose: true...") have their diagnostics
# already available next time a run fails, with no need to reproduce.
# Must be set *inside* the developer login shell — `sudo -i` scrubs
# the parent env. The live human-readable view of the conversation
# remains in claude-pretty.log via the renderer sidecar above
# (CW stream "claude").
sudo -i -u developer -- \
  bash -c "cd $WORKDIR && ANTHROPIC_LOG=debug claude --print --dangerously-skip-permissions < $WORKDIR/prompt.md" \
  > "$LOG_DIR/claude.log" 2>&1

CLAUDE_EXIT=$?
log "Claude exited with status $CLAUDE_EXIT"

# --------------------------------------------------------------------
# Self-terminate. terminate_self() is also wired to the ERR trap so
# any earlier failure has already invoked it. Outcome label feeds the
# Pushgateway summary so the dashboard's Run history panel can color
# rows by success vs error.
# --------------------------------------------------------------------
if [ "$CLAUDE_EXIT" -eq 0 ]; then
  terminate_self success
else
  terminate_self error
fi
