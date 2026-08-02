# Configurable Claude model + per-model metrics

**Date:** 2026-08-01
**Status:** implemented
**Builds on:** instance-type capture ([2026-06-17-instance-type-and-cost-metrics-design.md](2026-06-17-instance-type-and-cost-metrics-design.md), merged in PR #23)

## Problem

The worker invokes `claude --print` with no `--model`, so every autonomous run
uses whatever Claude Code defaults to. Two consequences:

1. **Not configurable.** Switching models — to spread load across a different
   rate-limit pool, or to try a more capable model on hard tickets — is not
   possible without editing `bootstrap/userdata.sh.tpl`.
2. **Not observable.** Nothing records which model authored a SOW or DX, so the
   dashboard cannot show the distribution of work by model the way it already
   does by instance type.

## Changes

### Model selection

The dispatch workflow resolves the model once, most-specific-first:

```
inputs.model  →  vars.DEVELOPER_CLAUDE_MODEL  →  "claude-fable-5"
```

- **`inputs.model`** — a new optional `workflow_dispatch` input. Blank (the
  default) means "use the repo default". This is the per-run override.
- **`vars.DEVELOPER_CLAUDE_MODEL`** — a GitHub Actions repository variable. This
  is the knob to turn when rate limits bite: editable in the GitHub UI, no PR, no
  deploy, effective on the next dispatch.
- **`claude-fable-5`** — a hardcoded floor so a repo with no variable set still
  dispatches. Chosen as the default because it is Anthropic's most capable
  widely released model and the platform's workload is long-horizon agentic work.

The resolved value is exposed as a step output and reused by validation, the
userdata render, and `fleet acquire`.

### Validation gate

`fleet/models.py` gains `DEFAULT_MODEL`, a `KNOWN_MODELS` frozenset, and a
`validate_model()` function; `fleet/cli.py` gains a `resolve-model` subcommand
that applies the default floor, validates, and prints the resolved ID (exiting
non-zero on an unknown value). The dispatch workflow captures its stdout
immediately after `uv sync` — before the fleet-cap check, before the lock,
before any instance. Folding the floor into the same command keeps
`claude-fable-5` in exactly one place rather than duplicating it into the
workflow YAML.

Seed set:

```
claude-fable-5, claude-opus-5, claude-opus-4-8, claude-sonnet-5
```

The gate fails closed. A typo'd model would otherwise boot a `t3.xlarge` that
dies ~4 minutes in when `claude` rejects the flag — cheap in dollars, but it
burns a dispatch cycle and the operator's attention, and the SOW lock churns.
The cost of failing closed is that adopting a brand-new model ID needs a one-line
PR to extend the set. That is the same trade `FLEET_CAP` already makes.

**Availability caveat.** The worker authenticates with Claude Code OAuth
credentials from Secrets Manager, so which models it can actually run is
subscription-gated. `KNOWN_MODELS` asserts "this is a real model ID we are
willing to run", not "this subscription serves it". Smoke-test a model on one
dispatch before making it the repo default.

### Threading the model to the worker

`bootstrap/userdata.sh.tpl` gains a `${claude_model}` placeholder and invokes:

```sh
claude --model '${claude_model}' --print --dangerously-skip-permissions
```

(Single-quoted: that command already sits inside a double-quoted `bash -c "..."`.)

Both renderers must supply the new key or they break:

- the dispatch workflow's hand-rolled `src.replace("${" + k + "}", v)` substitution;
- `terraform/ec2.tf`'s `templatefile()` for the launch template's baked userdata,
  which errors on any unsupplied variable. It gets `claude_model = ""`, matching
  how `sow_path` and `manager_private_ip` are already handled there (that baked
  userdata never runs in production — the workflow always overrides it).

### Capturing the model that actually ran

Recording the *dispatched* model would be simpler, but it lies in exactly the
case this feature exists for: when Claude Code falls back to a different model
under rate limits, the dashboard would still show what was asked for. So the
worker reports what it observed.

A new `/var/run/developer-worker/model` state file, mirroring `prs_opened`:

- **Seeded** with `${claude_model}` alongside `echo 0 > .../prs_opened`, before
  any failure-prone work runs. Guarantees every release path — including a boot
  failure long before Claude starts — reports something meaningful.
- **Overwritten** by an `observe_model()` helper with the last `message.model`
  seen in `/home/developer/.claude/projects/*/*.jsonl`. These are the same
  session event files the pretty-log renderer sidecar already tails, so this is
  one `jq` line, not new plumbing. Best-effort: a failed extraction leaves the
  seeded value in place.
- **Read** by `release_sow_lock()`, which calls `observe_model` and then passes
  `--model "$model"`.

**`observe_model` is called from `release_sow_lock`, not inline after the
`claude` invocation.** The inline placement was implemented first and rejected:
the script runs under `set -euo pipefail` with an ERR trap wired to
`terminate_self error`, so a nonzero `claude` exit fires the trap *before* any
following line executes. Capturing there would have recorded the dispatched
model on precisely the runs that matter most — a rate-limit fallback is most
likely to end in a failed or degraded run. Routing through `release_sow_lock`
puts the capture on both the clean-exit and ERR-trap paths.

### Persistence on the run-history row

`RunHistory` gains `model: str = "unknown"`.

- `try_acquire` writes the dispatched value, so an in-flight row
  (`outcome=working`) carries a real model rather than landing in `unknown`.
- `release` patches it with the observed value when one is supplied.
- `_to_history` reads `item.get("model", "unknown")`, so run rows written before
  this change keep deserializing — the same defaulting `compute_type` uses with
  `"ec2"`. Those rows will read `unknown` forever; they are not backfilled.

Threaded through `fleet/registry.py` (abstract), `fleet/dynamo.py`,
`fleet/memory.py`, and `fleet/cli.py` (`acquire --model`, `release --model`).

### Metrics

`fleet/metrics.aggregate` keys its inner loop on observed `(compute_type, model)`
pairs instead of `compute_type` alone. Three series gain a `model` label:

```
developer_history_runs_total{doc_type, compute_type, model, outcome}
developer_history_compute_seconds_total{doc_type, compute_type, model}
developer_history_compute_cost_dollars_total{doc_type, compute_type, model}
```

`PRS_OPENED_TOTAL` and the duration stats are unchanged — they stay keyed by
`doc_type` (and `outcome`) only.

Adding a label rather than minting a separate counter keeps one source of truth
for run counts and makes `model × doc_type` and `model × instance type` slices
answerable in a single PromQL query. Existing panels that `sum()` or
`sum by (doc_type)` are unaffected by the extra label.

Cardinality stays bounded: only observed pairs are emitted, and the model set is
small and gated by `KNOWN_MODELS`.

`bootstrap/ddb_exporter.py`'s `_LABELED` and `_HELP` maps follow.

### Dashboard (Lifetime / History section)

- New **By model (all-time)** table: Runs, Compute-time, Worker cost — the same
  shape as **By instance type**, which it sits beside, but the cost column is
  labeled **Worker cost** rather than **Est. cost** here (the other tables'
  row dimension already primes the reader to read "cost" as compute cost; a
  model row doesn't), and rows sort by Runs descending.
- The section's markdown text panel gains a line describing the model split.

Note the cost column is *worker* cost (EC2 wall-clock × hourly rate), not token
cost. It answers "which model burns more worker-hours", not "which model costs
more to run". Token spend is not visible to this platform.

### Docs

`README.md` gains a short model-selection note in the dispatch/quick-links area:
where the default lives, how to override per run, and how to extend
`KNOWN_MODELS`.

## Testing

- `fleet/models`: `validate_model` accepts every `KNOWN_MODELS` entry and rejects
  unknown/empty values.
- `fleet/metrics`: `model` label on all three series; observed-pair iteration
  (two models under one doc_type produce distinct series); rows defaulting to
  `unknown`; working rows still excluded from compute-time and cost.
- Registry/dynamo/memory/CLI suites: `model` round-trips through
  `acquire` → `release`; a history item with no `model` attribute deserializes to
  `unknown`.
- `ddb_exporter`: the new label sets and that `refresh` publishes per-model series.
- Manual: one dispatch on the default and one with the `model` input overridden;
  confirm the run-history row records the observed model and the dashboard table
  populates.

## Out of scope

- Backfilling `model` on rows written before this change (they read `unknown`).
- Token-cost metrics — the worker has no visibility into token spend.
- Per-work-type model defaults (e.g. DX on one model, SOW on another). The
  per-run input covers the ad-hoc case; a standing split can be added later if
  the distribution data justifies it.
- Windowed (30d/90d) variants, retry/stuck-run metrics, alerting — still deferred
  from the prior specs.
