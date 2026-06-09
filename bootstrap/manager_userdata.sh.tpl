#!/bin/bash
# prog-strength-developer-manager userdata (Graviton t4g.small, AL2023 arm64).
#
# Installs docker + compose, mounts the data EBS at /var/lib/manager,
# clones prog-strength-developer using a GitHub App installation token
# (private-repo-safe — mirrors the worker pattern), and runs
# `docker compose up -d`. Subsequent updates flow through
# .github/workflows/deploy-manager.yml via SSM.

set -euo pipefail
export HOME=/root

log() { echo "[manager-userdata $(date -u +%FT%TZ)] $*"; }
exec > >(tee -a /var/log/manager-userdata.log) 2>&1

# --------------------------------------------------------------------
# System packages: docker (for compose), git (for clone), python3 +
# pyjwt (for minting the GitHub App installation token), unzip (for
# the docker-compose plugin tarball below if we ever swap to that).
# --------------------------------------------------------------------
log "Installing system packages"
dnf update -y -q
dnf install -y -q docker git python3 python3-pip unzip
pip3 install --quiet 'pyjwt[crypto]>=2.8'
systemctl enable --now docker

# Docker compose plugin (v2) — arm64 binary, dropped into the CLI
# plugins dir so `docker compose ...` works.
log "Installing docker compose plugin"
mkdir -p /usr/local/lib/docker/cli-plugins
COMPOSE_VERSION=v2.30.3
curl -fsSL "https://github.com/docker/compose/releases/download/$${COMPOSE_VERSION}/docker-compose-linux-aarch64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version

# --------------------------------------------------------------------
# Data EBS volume: surfaces as /dev/sdf, /dev/xvdf, or /dev/nvme1n1
# depending on AL2023's nvme renaming. Format if blank, mount under
# /var/lib/manager, and add to fstab so it survives reboots.
# --------------------------------------------------------------------
log "Mounting data EBS at /var/lib/manager"
DATA_DEV=""
for cand in /dev/sdf /dev/nvme1n1 /dev/xvdf; do
  if [ -b "$cand" ]; then DATA_DEV=$cand; break; fi
done
if [ -z "$DATA_DEV" ]; then
  # Sometimes the device shows up a few seconds after instance boot;
  # poll briefly before giving up.
  for _ in $(seq 1 30); do
    sleep 2
    for cand in /dev/sdf /dev/nvme1n1 /dev/xvdf; do
      if [ -b "$cand" ]; then DATA_DEV=$cand; break 2; fi
    done
  done
fi
if [ -z "$DATA_DEV" ]; then
  log "FATAL: data EBS volume not visible at any expected path"
  exit 1
fi
if ! blkid "$DATA_DEV" >/dev/null 2>&1; then
  mkfs.ext4 -L manager-data "$DATA_DEV"
fi
mkdir -p /var/lib/manager
mount "$DATA_DEV" /var/lib/manager
echo "LABEL=manager-data /var/lib/manager ext4 defaults,nofail 0 2" >> /etc/fstab

# Pre-create per-service data dirs with the UID each container runs as.
# If docker compose auto-creates them on first 'up', they end up root-owned
# and the non-root containers (grafana=472, prometheus=nobody=65534,
# loki=10001) crash-loop on 'permission denied' against their bind mount.
# Pushgateway and Caddy run as root inside their images so their dirs are
# fine without this. install -d also fixes ownership on existing dirs,
# so re-running this on a manager that already booted in the broken
# state recovers without a wipe.
log "Creating per-service data dirs with container UIDs"
install -d -o 472   -g 0     /var/lib/manager/grafana
install -d -o 65534 -g 65534 /var/lib/manager/prometheus
install -d -o 10001 -g 10001 /var/lib/manager/loki

# --------------------------------------------------------------------
# Clone prog-strength-developer using a GitHub App installation token.
# The token-mint pattern mirrors bootstrap/userdata.sh.tpl on the worker
# so the manager's auth has the same shape and the same Secrets Manager
# entry powers both.
# --------------------------------------------------------------------
log "Fetching GitHub App credentials from Secrets Manager"
aws secretsmanager get-secret-value \
  --region "${aws_region}" \
  --secret-id "${github_app_secret_name}" \
  --query SecretString --output text > /root/.github-app.json
chmod 600 /root/.github-app.json

log "Minting GitHub App installation token"
cat > /root/mint-gh-token.py <<'PY'
import json, time, urllib.request
import jwt

with open('/root/.github-app.json') as f:
    cfg = json.load(f)

now = int(time.time())
payload = {"iat": now - 60, "exp": now + 540, "iss": str(cfg["app_id"])}
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
    print(json.loads(resp.read())["token"])
PY
GH_TOKEN=$(python3 /root/mint-gh-token.py)

log "Cloning prog-strength-developer"
git clone "https://x-access-token:$${GH_TOKEN}@github.com/${github_org}/prog-strength-developer.git" /opt/prog-strength-developer
unset GH_TOKEN

# --------------------------------------------------------------------
# Persist Grafana admin env so the SSM-driven deploy workflow's
# `docker compose up` sees the same values without re-reading state.
# --------------------------------------------------------------------
cat > /etc/profile.d/manager.sh <<EOF
export PROG_STRENGTH_DEVELOPER_DIR=/opt/prog-strength-developer
export GRAFANA_ADMIN_USER='${grafana_admin_user}'
export GRAFANA_ADMIN_PASSWORD='${grafana_admin_password}'
EOF
chmod 600 /etc/profile.d/manager.sh

# --------------------------------------------------------------------
# First-boot compose up. Subsequent updates flow through
# .github/workflows/deploy-manager.yml.
# --------------------------------------------------------------------
log "Starting docker compose stack"
export PROG_STRENGTH_DEVELOPER_DIR=/opt/prog-strength-developer
export GRAFANA_ADMIN_USER='${grafana_admin_user}'
export GRAFANA_ADMIN_PASSWORD='${grafana_admin_password}'
cd /opt/prog-strength-developer/monitoring
docker compose up -d
log "Manager bootstrap complete"
