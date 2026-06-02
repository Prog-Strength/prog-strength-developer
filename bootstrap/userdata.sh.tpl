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
terminate_self() {
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
  # Flush CloudWatch agent buffer before disappearing.
  sleep 10
  if [ -n "$iid" ]; then
    aws ec2 terminate-instances \
      --region "${aws_region}" \
      --instance-ids "$iid" || true
  fi
}

trap 'log "FATAL: userdata exited at line $LINENO with status $?"; terminate_self' ERR

INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token)" \
  http://169.254.169.254/latest/meta-data/instance-id)
export INSTANCE_ID

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

# Install uv (Python project + tool manager).
log "Installing uv"
curl -fsSL https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:/root/.local/bin:$PATH"

# Install Claude Code CLI via the official npm package.
log "Installing Claude Code"
npm install -g --silent @anthropic-ai/claude-code

# --------------------------------------------------------------------
# Configure CloudWatch agent to ship /var/log/prog-strength-developer
# to the developer log group, one stream per instance ID.
# --------------------------------------------------------------------
log "Configuring CloudWatch agent"
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "$LOG_DIR/*.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$INSTANCE_ID",
            "timestamp_format": "%Y-%m-%dT%H:%M:%SZ",
            "retention_in_days": -1
          },
          {
            "file_path": "/var/log/cloud-init-output.log",
            "log_group_name": "${log_group_name}",
            "log_stream_name": "$INSTANCE_ID-cloud-init",
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

log "Rendering Claude prompt for SOW ${sow_path}"
SOW_SLUG=$(basename "${sow_path}" .md)
sed \
  -e "s|__SOW_PATH__|${sow_path}|g" \
  -e "s|__SOW_SLUG__|$SOW_SLUG|g" \
  -e "s|__GITHUB_ORG__|${github_org}|g" \
  /opt/prog-strength-developer-repo/bootstrap/prompt.md.tpl \
  > "$WORKDIR/prompt.md"

# Transfer the workspace to the developer user so claude (running as
# developer) can read the prompt + cloned repos and write back the
# branches/files it produces.
chown -R developer:developer "$WORKDIR"

# Diagnostic: confirm developer's claude credentials are in place
# before invoking claude. A missing file here means the OAuth blob
# never reached the worker, which is a one-line failure mode worth
# surfacing distinctly from a "claude ran but auth failed" failure.
log "Developer claude credentials present:"
sudo -u developer ls -la "$DEV_HOME/.claude/" 2>&1 || true

log "Starting Claude Code (as non-root user 'developer')"
# --print is non-interactive batch mode.
# --dangerously-skip-permissions waives the interactive permission
#   prompts that would otherwise block headless operation. claude
#   refuses this flag under root, so we drop into the developer user
#   via sudo for just this command.
# sudo -i runs as a login shell so $HOME is /home/developer and the
# default PATH includes the npm-global bin where claude installed.
sudo -i -u developer -- bash -c "cd $WORKDIR && claude --print --dangerously-skip-permissions < $WORKDIR/prompt.md" \
  | tee "$LOG_DIR/claude.log"

CLAUDE_EXIT=$?
log "Claude exited with status $CLAUDE_EXIT"

# --------------------------------------------------------------------
# Self-terminate. terminate_self() is also wired to the ERR trap so
# any earlier failure has already invoked it.
# --------------------------------------------------------------------
terminate_self
