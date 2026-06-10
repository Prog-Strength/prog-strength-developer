# Prog Strength Developer — Agent Contributor Guide

This file is for AI coding agents (Claude, Copilot, Codex, Gemini, etc.)
making contributions to `prog-strength-developer`. Human contributors
should start with [README.md](README.md) and the `docs/` runbooks.

There is a meta dimension here. An agent editing this repo is often
either (a) running locally in the owner's Claude Code session, or (b)
running INSIDE this very system as an ephemeral EC2 worker that this
repo's Terraform launches. Edits that change worker behavior change
the conditions under which future agents in this repo will run.

## What this project is

`prog-strength-developer` is the **autonomous developer** for the Prog
Strength stack. It dispatches ephemeral EC2 workers that run Claude
Code unattended against statements of work (SOWs) from
`prog-strength-docs`, open pull requests across every affected repo,
and self-terminate. After the developer-manager work shipped, it also
hosts a permanent **manager** instance that exposes the platform's
operational dashboards over HTTPS.

This is **infrastructure + automation only**. There is no application
business logic here. Application code lives in the sibling repos
(`prog-strength-api`, `prog-strength-mcp`, `prog-strength-agent`,
`prog-strength-web`, `prog-strength-mobile`). Infrastructure for the
application itself lives in `prog-strength-infra`. The boundary
between this repo and `prog-strength-infra` is deliberate: application
infra and developer-platform infra are different domains, isolated by
VPC and by ownership.

The two SOWs that define this system end-to-end live in
`prog-strength-docs/sows/`:

- `prog-strength-developer.md` — original v1 system (workers, dispatch,
  single-instance).
- `developer-manager-and-concurrent-workers.md` — manager, concurrent
  workers, dashboards, live log tail.

Read those before doing non-trivial work in this repo.

## Repo layout

```
terraform/    AWS resources: VPC, subnets, SGs, IAM, launch template,
              CloudWatch log group, Secrets Manager references,
              manager EC2 + EBS + EIP.
bootstrap/    Cloud-init userdata templates (worker + manager) and the
              Python worker_exporter. Userdata is rendered at apply
              time by Terraform for the launch template's base copy,
              and re-rendered at dispatch time by the workflow's
              Python render step for each actual worker.
monitoring/   Manager docker-compose stack: Prometheus, Grafana,
              Pushgateway, Caddy, cAdvisor, node_exporter, Loki.
              Plus Prometheus scrape config and Grafana provisioning
              (datasources + auto-loaded dashboards).
caddy/        Caddyfile for the manager. Terminates TLS at
              developers.progstrength.fitness and reverse-proxies
              Grafana. (The application-host Caddy lives in
              prog-strength-infra and cannot reach the manager across
              the VPC boundary.)
scripts/      Small helpers. Keep this directory shallow.
tests/        pytest for the worker_exporter — the only code in this
              repo that benefits from unit tests.
.github/      Workflows: apply.yml (terraform apply on push to main),
                         plan.yml (terraform plan on every PR),
                         dispatch-sow.yml (manual: launches a worker),
                         deploy-manager.yml (rolls compose changes onto
                         the manager via SSM),
                         release.yml (semantic-release).
docs/         README, setup.md, troubleshooting.md.
```

## Architecture (post-manager merge)

Two classes of compute, one VPC:

- **Manager** (permanent). `t4g.small` (Graviton, arm64), AL2023.
  Sits in its own public subnet of the developer VPC. Runs a docker-
  compose stack: Prometheus + Pushgateway + Grafana + Caddy +
  node_exporter + cAdvisor + Loki. Holds 15 days of TSDB and 7 days
  of Loki logs on a 20 GB gp3 data volume mounted at
  `/var/lib/manager`. Reachable at `https://developers.progstrength.fitness`
  via Caddy.

- **Workers** (ephemeral). `t3.large`, x86_64, AL2023. Launched on
  demand by the dispatch workflow via `aws ec2 run-instances`. The
  worker EC2 itself is **NOT in Terraform** — it used to be, but the
  shared `terraform-apply-prod` state lock made concurrent dispatches
  serialize. Now only the launch template, IAM, VPC, secrets, log
  group, and manager live in Terraform; workers are pure API calls.

The developer VPC has **no peering** to the application VPC in
`prog-strength-infra`. This is deliberate: a misbehaving worker
cannot reach prod. Don't propose peering.

## Working on this repo as an agent

A few rules that have come up enough to be worth stating up-front:

- **Default to verify, not assume.** This is infrastructure — a wrong
  Terraform or compose change can spin up real AWS resources or break
  the manager. Run `terraform validate` and `terraform fmt` after
  every `.tf` edit. Run `python3 -c "import json; json.load(open(...))"`
  on dashboard JSON. Run `uv run pytest -q` after touching
  `worker_exporter.py` or its tests.
- **TDD applies to the worker_exporter.** That's the only first-class
  application code. Tests in `tests/test_worker_exporter.py`. For
  IaC, "verify" steps are validation commands, not unit tests.
- **Every commit AND every PR title must be a Conventional Commit.**
  Format: `<type>(<scope>): <imperative subject>`. Recent history uses
  `feat(scope): ...`, `fix(scope): ...`, `docs: ...`, `ci(scope): ...`.
  Match the pattern. Releases are cut by `semantic-release` on every
  push to `main`, driven entirely by commit subjects: `feat:` bumps
  minor, `fix:`/`perf:` bump patch, `!` or `BREAKING CHANGE:` bump
  major, everything else (`docs`, `ci`, `chore`, `refactor`, `test`)
  cuts no release. **The PR title is the gotcha**: GitHub's
  squash-merge button uses the PR title as the merge commit subject,
  so an unconventional title produces an unconventional commit on
  `main` no matter how clean the individual commits were — and the
  release that PR was supposed to ship is lost. The full conventions,
  including allowed scopes and worked examples, are in `CONTRIBUTING.md`.
  Read it once.
- **Don't push directly to main.** Every change flows through a PR;
  plan.yml posts the diff as a sticky comment. Merging triggers
  apply.yml which provisions the change.
- **Match the existing style.** Short comments explain WHY a thing is
  the way it is, especially in `bootstrap/userdata.sh.tpl` where the
  same script is rendered by both Terraform's `templatefile()` and
  the dispatch workflow's Python substitution — comments protect the
  next reader from misinterpreting the escape rules.
- **Stay scoped.** Bug fixes do not need "while I'm here" refactors.
  IaC compounds tightly; an "improvement" can quietly change resource
  identity and force a recreate.

## Sharp edges (recurring footguns)

These have all bitten work in this repo:

- **AWS Security Group description regex.** Descriptions must match
  `^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$`. That excludes `<`, `>`,
  `'`, `"`, and backtick. Plan-on-PR will fail with a regex error if
  you use any of them. The same applies to many other tag-value
  fields; default to ASCII letters, digits, dot, dash, space, and
  parentheses.
- **Userdata template has two render paths.** `bootstrap/userdata.sh.tpl`
  is rendered by:
  1. Terraform's `templatefile()` (for the launch template's baked
     userdata). `${name}` is substituted; `$${...}` is unescaped to
     `${...}` so bash sees it at runtime.
  2. The dispatch workflow's Python step (for the userdata that
     actually runs on a dispatched worker). The Python does the same
     two passes — named `${name}` substitution from a dict, then a
     final `src.replace("$${", "${")` to mirror Terraform's unescape.
  If you add new `${...}` placeholders, add them to the workflow's
  Python `subs` dict. If you add new `$${...}` escapes (literal
  `${VAR}` that bash should evaluate), no workflow change needed.
- **The JSONL renderer pipeline is load-bearing.** Claude Code in
  `--print` mode emits NOTHING to stdout — every assistant turn,
  `tool_use`, and `tool_result` lands in `~/.claude/projects/<slug>/<uuid>.jsonl`.
  The renderer sidecar at the bottom of `userdata.sh.tpl` tails those
  JSONLs through `jq`, decodes each event to one timestamped line,
  and appends to `/var/log/prog-strength-developer/claude-pretty.log`.
  That file is what the CloudWatch agent ships as the `claude` stream
  AND what Promtail ships to Loki for the "Live Claude output" panel.
  Don't try to replace the renderer with a stdout pipe — claude is
  silent on stdout. See `docs/troubleshooting.md` and the
  `developer_logging_pipeline` auto-memory note.
- **The worker EC2 is not in Terraform.** Don't add
  `aws_instance.worker` back. The launch template stays; the dispatch
  workflow calls `aws ec2 run-instances` directly.
- **Worker self-terminate IAM uses `ec2:SourceInstanceARN` with
  `ArnEquals`.** Each worker can terminate ONLY itself. This is what
  makes concurrent workers safe in the same SG. Don't loosen the
  condition to a tag-based match.
- **Manager DNS lives in GoDaddy, not Terraform.** Terraform allocates
  the EIP and exposes it as the `manager_public_ip` output. The
  operator copies it into GoDaddy's DNS panel manually. If you find
  yourself reaching for `aws_route53_record`, stop — the
  `progstrength.fitness` zone is NOT in Route 53.
- **Caddy 404s `/metrics`.** Both Caddyfiles (this repo and infra)
  block `/metrics` on every public vhost so Grafana's self-scrape
  endpoint isn't internet-reachable. Don't punch a hole through that.
- **Pushgateway uses `honor_labels: true`.** Workers push under
  `/metrics/job/developer_run/instance/<instance_id>` and the
  instance label MUST survive scraping. Don't change `honor_labels`
  without thinking through what gets clobbered.
- **The manager clones via a GitHub App token.** Same Secrets Manager
  entry as the worker. If you add new files the manager needs to
  read on boot, they must be in this repo's git tree — the manager
  doesn't have a separate distribution channel.

## Architecture decisions (settled — don't relitigate)

- **One VPC, two subnets, no peering to the application VPC.** The
  worker subnet hosts ephemeral workers; the manager subnet hosts the
  permanent manager. They share a route table but isolate via SG.
- **The developer-platform Caddy runs on the manager.** It cannot be
  reverse-proxied through the application-host Caddy because the two
  VPCs don't peer.
- **Workers x86_64, manager arm64.** Worker stays x86 because some
  Claude Code/Node tooling has uneven arm support; manager is
  Graviton because Prometheus + Grafana run beautifully on arm and
  the cost/perf is better. Don't unify the architectures unless one
  of those reasons changes.
- **Soft fleet cap (default 10) in the dispatch workflow.** Not a
  hard limit; raise via a one-line PR. The cap exists to make a
  runaway dispatch loop visible before it burns dollars.
- **Pushgateway over a custom HTTP receiver.** Loose coupling, no
  bespoke code on the manager, canonical pattern for ephemeral batch
  jobs publishing final stats.
- **Loki + Promtail over fluent-bit/Vector.** Same Grafana datasource
  model as Prometheus; the live-tail UX is built in. Promtail is in
  maintenance mode upstream but is meaningfully smaller than Grafana
  Alloy for the single-file-tail use case.

## Deliberately deferred

Considered and not on the roadmap. Push back / ask before adding any
of these:

- **Alerting.** No Alertmanager, no Slack/PagerDuty webhooks. The
  Grafana dashboards are the operational surface; at single-operator
  scale, push notifications about a side project would be noise.
- **HA manager.** One manager instance, no failover. The cost of an
  hour of manager downtime is near zero — workers keep running and
  CloudWatch is the source of truth for any in-flight run.
- **VPC peering or shared infrastructure with the application stack.**
  Isolation is the point.
- **Auto-discovery of SOWs.** The owner explicitly dispatches by
  path. No "scan for ready_for_implementation status and queue them"
  loop.
- **Multi-tenant / cross-org workers.** Single org (`Prog-Strength`),
  single secret set.
- **OAuth/SSO in front of Grafana.** Built-in admin user/password is
  adequate for single-operator beta; setup notes are in
  `docs/setup.md`.
- **Removing the soft fleet cap.** It stays as a budget guardrail.
- **Per-SOW cost attribution.** Cost panel sums fleet-wide compute
  time; per-SOW chargeback needs real CAT plumbing.

## Code style

- **Bash userdata**: `set -euo pipefail` at top; every block prefixed
  with a `log` line; `terminate_self` wired through both the ERR
  trap and every success path. Heredocs use `<<'EOF'` when expansion
  must NOT happen (e.g., embedded Python scripts) and `<<EOF` when
  shell vars should expand at write time.
- **Terraform**: `terraform fmt` before every commit. Resource names
  in snake_case; resource Name tags use the
  `prog-strength-developer-<role>[-<thing>]` pattern.
- **Python**: tests live next to fixtures under `tests/`. The
  worker_exporter's public surface (`parse_jsonl_events`,
  `read_state_file`, `read_prs_opened`, `ExporterState`) is what
  tests cover.
- **Comments explain WHY, not WHAT.** Reserve them for non-obvious
  design choices (escape rules, IAM scoping, render paths) that a
  future reader would otherwise have to re-derive.
- **No emoji or decorative ASCII** in code, comments, or commit
  messages.

## When in doubt

- Ask before changing one of the architecture decisions above.
- Default to small, reviewable changes over sweeping IaC restructures.
- If you're about to suppress a `terraform plan` warning, silence a
  GHA lint, or bypass a pre-commit hook, write up why in the PR
  rather than hiding it.
- Read the auto-memory notes if you have access to them — there are
  several developer-specific notes (e.g. `developer_logging_pipeline`,
  `feedback_automation_over_manual`) that explain decisions in more
  depth.

## Reference

- v1 SOW: `prog-strength-docs/sows/prog-strength-developer.md`
- Manager SOW: `prog-strength-docs/sows/developer-manager-and-concurrent-workers.md`
- Plans: `prog-strength-docs/plans/2026-06-08-developer-manager-and-concurrent-workers.md`
- Manager dashboard: <https://developers.progstrength.fitness/d/manager-host-health>
- Developer Platform dashboard: <https://developers.progstrength.fitness/d/developer-platform>
- Dispatch workflow: <https://github.com/Prog-Strength/prog-strength-developer/actions/workflows/dispatch-sow.yml>
- Application monitoring (for comparison): <https://monitoring.progstrength.fitness>
