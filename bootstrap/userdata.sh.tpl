#!/bin/bash
# prog-strength-developer worker userdata.
# Rendered by Terraform's templatefile() — variables wrapped in
# $${...} are substituted at apply time.
#
# Lifecycle: install deps → fetch secrets → clone repos → run Claude
# Code → self-terminate. Every branch terminates the instance,
# including failures. A systemd timer is set as a hard backstop.

set -euo pipefail

LOG_DIR=/var/log/prog-strength-developer
mkdir -p "$LOG_DIR"
WORKDIR=/workspace
mkdir -p "$WORKDIR"

# Tee all subsequent output so the CloudWatch agent picks it up.
exec > >(tee -a "$LOG_DIR/userdata.log") 2>&1

log() { echo "[userdata $(date -u +%FT%TZ)] $*"; }

trap 'log "FATAL: userdata exited at line $LINENO with status $?"; terminate_self' ERR

INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $(curl -fsS -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
  http://169.254.169.254/latest/api/token)" \
  http://169.254.169.254/latest/meta-data/instance-id)
export INSTANCE_ID

terminate_self() {
  log "Terminating instance $INSTANCE_ID"
  # Flush CloudWatch agent buffer before disappearing.
  sleep 10
  aws ec2 terminate-instances \
    --region "${aws_region}" \
    --instance-ids "$INSTANCE_ID" || true
}

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
# Fetch secrets.
# --------------------------------------------------------------------
log "Fetching Claude credentials from Secrets Manager"
mkdir -p /root/.claude
aws secretsmanager get-secret-value \
  --region "${aws_region}" \
  --secret-id "${claude_secret_name}" \
  --query SecretString --output text > /root/.claude/credentials.json
chmod 600 /root/.claude/credentials.json

log "Fetching GitHub App credentials"
aws secretsmanager get-secret-value \
  --region "${aws_region}" \
  --secret-id "${github_app_secret_name}" \
  --query SecretString --output text > /root/.github-app.json
chmod 600 /root/.github-app.json

# Mint an installation token. mint-gh-token.sh ships in this repo; the
# launch template embedded it via cloud-init's write_files mechanism in
# an earlier revision — for v1 we inline the minimal Python here to
# avoid the second-asset coordination problem.
log "Minting GitHub App installation token"
GH_TOKEN=$(python3 - <<'PY'
import json, os, time, base64, urllib.request, sys
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
)
echo "$GH_TOKEN" | gh auth login --with-token

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
# --------------------------------------------------------------------
log "Writing prompt template to disk"
# The prompt template is embedded via Terraform's templatefile(); the
# single-quoted heredoc keeps bash from interpreting any $-prefixed
# tokens inside the prompt itself.
mkdir -p /opt/prog-strength-developer
cat > /opt/prog-strength-developer/prompt.md.tpl <<'PROMPT_TEMPLATE_EOF'
${prompt_template}
PROMPT_TEMPLATE_EOF

log "Rendering Claude prompt for SOW ${sow_path}"
SOW_SLUG=$(basename "${sow_path}" .md)
sed \
  -e "s|__SOW_PATH__|${sow_path}|g" \
  -e "s|__SOW_SLUG__|$SOW_SLUG|g" \
  -e "s|__GITHUB_ORG__|${github_org}|g" \
  /opt/prog-strength-developer/prompt.md.tpl \
  > "$WORKDIR/prompt.md"

cd "$WORKDIR"
log "Starting Claude Code"
# --print is non-interactive batch mode.
# --dangerously-skip-permissions waives the interactive permission
#   prompts that would otherwise block headless operation.
claude --print --dangerously-skip-permissions < prompt.md \
  | tee "$LOG_DIR/claude.log"

CLAUDE_EXIT=$?
log "Claude exited with status $CLAUDE_EXIT"

# --------------------------------------------------------------------
# Self-terminate. terminate_self() is also wired to the ERR trap so
# any earlier failure has already invoked it.
# --------------------------------------------------------------------
terminate_self
