# prog-strength-developer

Autonomous developer platform for [Prog Strength](https://github.com/Prog-Strength). Two halves:

- A permanent **manager** (`t4g.small`, Graviton) hosts the observability stack — Prometheus, Grafana, Loki, Pushgateway, Caddy — at [developers.progstrength.fitness](https://developers.progstrength.fitness).
- Ephemeral **workers** (`t3.xlarge`) spin up on demand, run Claude Code against a designated SOW from [prog-strength-docs](https://github.com/Prog-Strength/prog-strength-docs), open PRs in each affected repo, and self-terminate.

See `docs/README.md` for the full system overview and `docs/setup.md` for first-time bootstrap (including the one-time manager DNS + Grafana credential steps).

## Work types

The platform dispatches two work types. A "work type" is just three things — the ticket schema parsed from frontmatter, the prompt template the worker renders, and the branch/PR/merge contract that prompt enforces. Everything else (the dispatch workflow, the DynamoDB SOW lock, the userdata bootstrap, the dashboards) is type-agnostic plumbing both types reuse unchanged. The type is selected by a `type:` frontmatter field on the ticket (`sow` by default when absent, so every existing SOW keeps working untouched), and the worker **routes on it** in `bootstrap/ticket.py` — choosing the template and substituting the right tokens.

| | **SOW** (`type: sow`, default) | **DX** (`type: dx`) |
|---|---|---|
| Shape | Convergent — one spec, one correct implementation | Divergent — N differentiated visual variants of one frontend surface, side by side |
| Ticket | `sows/<feature>.md` | `dx/<surface>.md` |
| Prompt | `bootstrap/prompt.md.tpl` | `bootstrap/prompt-dx.md.tpl` (leans on the `frontend-design` skill) |
| Branch | `feat/<slug>` | throwaway `dx/<surface>` |
| Outcome | PRs that **merge** after review | a **draft `[DX — DO NOT MERGE]`** PR that is the selection artifact and **never merges** |
| "Done" signal | the `prog-strength-docs` status-flip PR is the operator's ready-to-ship signal | the human picks a variant at the selection gate, closes the PR, and writes a SOW to build the winner |

A DX produces a single comparison route (`/design-explore/<surface>`, behind a feature flag) rendering one disposable variant per enumerated idiom, so the output is a forced spread rather than N near-duplicates. It is dispatched exactly like a SOW — same "Dispatch ticket" workflow, same fleet lock, same dashboards — by pointing it at a `dx/<surface>.md` ticket. The selected variant then feeds a normal SOW that implements it production-quality, so the divergent exploration converges back into the convergent pipeline.

**Boot-time DX validation.** `bootstrap/ticket.py` validates a `type: dx` ticket before Claude runs: `surface` present, `references` non-empty, and `idioms` enumerated with `len(idioms) >= variant_count`. Without enumerated idioms the variants collapse into near-duplicates, so a malformed DX fails fast at boot with a clear message instead of after a wasted six-hour run.

**v1 is web-only.** DX targets `prog-strength-web` (Next.js) surfaces because the draft-PR handoff relies on the per-PR **preview deploy** to render the comparison. `prog-strength-mobile` (TestFlight, no per-PR web preview) needs a different handoff and is a noted future extension.

A convergent counterpart that harvests recurring decisions out of completed DXs into a durable design system (`design-system.md` + tokens) — a **DS (Design System)** work type — is deliberately deferred: there is nothing to harvest until several DXs have run, and a DS update may turn out to be just a normal SOW. See the DX SOW for the seam.

## Architecture

One VPC, two public subnets, no peering to the application VPC in `prog-strength-infra` — a misbehaving worker cannot reach prod.

**Manager.** Permanent `t4g.small` arm64 instance in its own subnet. Runs a docker-compose stack: Prometheus + Pushgateway + Grafana + Caddy + Loki + node_exporter + cAdvisor. Holds 15 days of TSDB and 7 days of Loki logs on a 20 GB gp3 data volume mounted at `/var/lib/manager`, so a manager replacement preserves dashboards, metrics, and Caddy certs. Caddy terminates TLS for `developers.progstrength.fitness` and reverse-proxies Grafana; cert is auto-provisioned by Let's Encrypt on first request. A stable Elastic IP keeps DNS the same across instance replacements — the `developers` A record in GoDaddy is a one-time setup step.

**Workers.** Ephemeral `t3.xlarge` x86_64 instances in a separate subnet, no inbound (SSM Session Manager only). Each worker boots `node_exporter` + `worker_exporter` (scraped by the manager via Prometheus `ec2_sd_config`), runs Claude Code in `--print` mode against one SOW, ships its live Claude output to Loki on the manager via Promtail, pushes a final-state run summary to the manager's Pushgateway on `:9091` before terminating.

**Why split.** The worker EC2 is **not** Terraform-managed — the shared `terraform-apply-prod` state lock made concurrent dispatches serialize. Workers are now pure `aws ec2 run-instances` calls against a persistent launch template, so the only ceiling is the soft fleet cap (default 10) in the dispatch workflow.

## Quick links

- **Dispatch a ticket:** Actions tab → "Dispatch ticket" → Run workflow → paste the ticket path (`sows/foo.md` or `dx/foo.md`). Runs in parallel up to a fleet cap of 10. See [Work types](#work-types).
- **Live dashboard:** <https://developers.progstrength.fitness/d/developer-platform> — fleet overview, per-worker drill-down, run history, live Claude log tail.
- **Manager host health:** <https://developers.progstrength.fitness/d/manager-host-health> — CPU/memory/disk/network/per-container metrics for right-sizing the manager itself.
- **CloudWatch logs:** `/aws/ec2/prog-strength-developer/<instance-id>`.
- **Debug a stuck worker:** `aws ssm start-session --target <instance-id>`.
- **See / clear SOW locks:** `uv run python -m fleet list` to see what's building; `uv run python -m fleet release --sow sows/<name>.md --instance-id none --outcome error --force` to free a stuck lock (see [Fleet control](#fleet-control)).
- **SSM into the manager:** `aws ssm start-session --target $(aws ec2 describe-instances --filters Name=tag:Name,Values=prog-strength-developer-manager Name=instance-state-name,Values=running --query 'Reservations[].Instances[].InstanceId' --output text)`

## Fleet control

The dispatch pipeline guarantees **at most one active worker per SOW**. Before launching anything, the "Dispatch SOW" workflow acquires a lock on the SOW in a DynamoDB **run registry** (`prog-strength-developer-runs` — one item per SOW path). If a worker is already building that SOW, the dispatch is refused and **no instance is launched**. That stops an accidental double-dispatch (a mis-click, an over-eager re-run of a workflow thought to have failed) from fanning out duplicate workers that race each other into conflicting PRs — and from burning the EC2 + Claude cost of doing so. The worker releases the lock when it finishes; a stale lock (a worker that died without releasing) self-heals via the row's `expires_at` TTL.

The locking logic deliberately does **not** live in the workflow YAML. It is a small, testable Python package — `fleet/` — that the workflow and the worker call as a thin CLI (`python -m fleet …`). The run registry is an interface (`fleet/registry.py`) with a DynamoDB implementation (`fleet/dynamo.py`) and an in-memory one for tests (`fleet/memory.py`), mirroring the application API's repository pattern. **This package is the intended home for future control-plane logic** (worker lifecycle, scheduling, assignment policy) — extend it there, not in the dispatch workflow, which stays a thin caller.

Lifecycle:

- **acquire** — dispatch workflow, before launch. An atomic DynamoDB conditional write, so two simultaneous dispatches cannot both win. Exit code `3` means "already in progress" (distinct from a real error).
- **attach** — dispatch workflow, after launch. Records the instance id on the lock.
- **release** — worker, on finalize (the same step that pushes summary metrics to Pushgateway). Frees the SOW. If release is ever missed (a hard crash), the lock's `expires_at` TTL reclaims the SOW — enforced in the acquire condition, not by relying on DynamoDB's best-effort TTL deletion.

Operator commands (need AWS credentials with access to the registry table):

```bash
# What's building right now?
uv run python -m fleet list

# Free a stuck SOW lock so it can be re-dispatched.
uv run python -m fleet release --sow sows/<name>.md --instance-id none --outcome error --force
```

## SOW

This repo implements `prog-strength-docs/sows/prog-strength-developer.md`.

## Infrastructure

Terraform changes flow through PRs:

- Opening a PR runs `terraform plan` and posts a sticky comment showing what would change. Don't merge without that comment saying either "No changes" or a reviewed diff.
- Merging to main runs `terraform apply -auto-approve` against the persistent infra: VPC + subnets + IAM, the worker launch template, the manager instance + its EBS data volume + Elastic IP, secrets, and the log group. Worker instances themselves are still launched separately via "Dispatch SOW".

Manager **config** changes — anything under `monitoring/**` or `caddy/**` — bypass Terraform and flow through `deploy-manager.yml` instead, which SSMs into the manager and re-runs `docker compose up -d`. No instance replacement, no Grafana restart (its file provider picks up dashboard edits within ~10s).

The plan/apply, dispatch-SOW, and release workflows share the `terraform-apply-prod` concurrency group so they queue on the state lock instead of racing. `deploy-manager` runs independently — it only touches docker compose on a single instance, not Terraform state.

## Releases

Versioning is automated via [semantic-release](https://semantic-release.gitbook.io). Every push to main analyzes conventional-commit subjects, picks the next semver bump, writes `CHANGELOG.md`, creates a GitHub Release with notes, and pushes a `vX.Y.Z` tag.

One-time bootstrap (must run **before** the first PR merges to main after this is enabled):

```bash
git checkout main && git pull
git tag -a v0.0.0 -m "Initial baseline"
git push origin v0.0.0
```

Without this baseline, semantic-release would default the first release to `1.0.0`. The `v0.0.0` tag pins it to the `0.x` range until the project is ready to declare a stable API.
