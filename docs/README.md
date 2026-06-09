# prog-strength-developer

An autonomous developer for [Prog Strength](https://github.com/Prog-Strength). Picks
up a designated SOW from `prog-strength-docs`, spins up an ephemeral EC2 worker
in a dedicated VPC, runs Claude Code unattended against that SOW, opens PRs in
every affected repo, and self-terminates.

Specification: `prog-strength-docs/sows/prog-strength-developer.md`.

## What this repo contains

- `terraform/` — IaC for the AWS resources (VPC, IAM, EC2 launch template,
  CloudWatch log group, Secrets Manager references).
- `bootstrap/` — the cloud-init userdata template and the Claude Code prompt
  template, both rendered at apply time with the SOW path baked in.
- `scripts/` — small helpers used by the GitHub Actions workflow.
- `.github/workflows/dispatch-sow.yml` — the manual `workflow_dispatch` entry
  point. Takes `sow_path` as input.
- `docs/` — system overview (this file), one-time bootstrap runbook, and
  troubleshooting notes.

## How a run works

1. You write a SOW in `prog-strength-docs/sows/<name>.md` with YAML
   frontmatter listing the affected `repos:`.
2. You open this repo on GitHub → Actions → "Dispatch SOW" → Run
   workflow → paste the SOW path (e.g. `sows/foo.md`). Concurrent
   dispatches are fine; the soft fleet cap of 10 is the only ceiling.
3. The workflow:
   - Assumes the AWS GHA OIDC role.
   - Checks the soft fleet cap against `aws ec2 describe-instances`.
   - Renders `bootstrap/userdata.sh.tpl` with `sow_path` and the
     manager's private IP baked in.
   - Calls `aws ec2 run-instances` against the persistent launch
     template. Exits. Does NOT wait for the worker.
4. The EC2 worker boots, installs deps, starts `node_exporter` and
   `worker_exporter` (visible on the Grafana dashboard within ~30
   seconds), fetches Claude OAuth credentials and a GitHub App
   installation token from Secrets Manager, clones the SOW + every
   repo listed in `repos:`, runs Claude Code, opens PRs in each
   modified repo, pushes a final-run summary (duration, outcome, PRs
   opened) to the manager's Pushgateway, and self-terminates.
5. You watch progress on the Grafana dashboard at
   <https://developers.progstrength.fitness> — Developer Platform for
   the fleet view, Manager Host Health for the box itself.
6. You review and merge the resulting PRs at your own pace.

## What the worker has access to

- The two Secrets Manager entries (`prog-strength-developer/claude-credentials`,
  `prog-strength-developer/github-app`) and nothing else.
- The ability to terminate EC2 instances tagged `Name=prog-strength-developer-worker`
  (i.e. itself).
- The dedicated CloudWatch log group.
- All outbound internet (GitHub, Anthropic, AWS, package registries).

It is on a separate VPC with no peering to the application VPC. It cannot
reach the prod API/MCP/DB.

## What the worker does NOT do

- Merge PRs (you do).
- Notify on completion (PRs visible in GitHub + the Grafana run history are the signal).
- Modify repos outside the `repos:` list in the SOW frontmatter.

## First-time setup

See [`setup.md`](./setup.md) for the bootstrap runbook.

## Debugging

See [`troubleshooting.md`](./troubleshooting.md).

## Cost

Roughly $0.30 per SOW run (mostly EC2 time on `t3.large`), plus ~$1/month of
always-on costs (Secrets Manager + log retention). A $50/month AWS budget alarm
fires before anything anomalous.
