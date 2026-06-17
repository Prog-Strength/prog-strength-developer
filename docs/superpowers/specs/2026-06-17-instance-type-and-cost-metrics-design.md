# Instance-type capture + cost metrics

**Date:** 2026-06-17
**Status:** implemented
**Builds on:** the run-history exporter ([2026-06-16-ddb-history-exporter-design.md](2026-06-16-ddb-history-exporter-design.md), merged in PR #22)

## Problem

After PR #22, `compute_type` on every run-history row was the coarse literal
`"ec2"` (set at `acquire`, before the instance exists) and was never exposed as a
metric label. So the dashboard had no instance-type breakdown and no cost view.

## Changes

### Capture the instance type
`attach_instance` gains an optional `compute_type`. The dispatch workflow's attach
step resolves the launched instance type (`describe-instances … InstanceType`, a
permission the GHA role already has) and passes `--compute-type ec2:<type>`, which
upgrades the coarse value on the run-history row. Best-effort: if the lookup fails
the row keeps `"ec2"`, and attach is already `continue-on-error`.

### Instance-type + cost metrics
`fleet/metrics.aggregate` now breaks `runs_total` and `compute_seconds_total` down by
`compute_type` (in addition to `doc_type`/`outcome`), and emits a new
`developer_history_compute_cost_dollars_total{doc_type, compute_type}`:

```
cost = Σ_terminal( duration_seconds / 3600 × hourly_rate(compute_type) )
```

`hourly_rate` reads a hardcoded us-east-2 on-demand price map (`t3.large` 0.0832,
`t3.xlarge` 0.1664, `t3.2xlarge` 0.3328) keyed by the instance type after the `ec2:`
prefix. An unpriced or coarse `ec2` row contributes $0. The map is a deliberate
shortcut — the fleet is small and single-region, so a static table beats a Pricing
API call; extend it as the fleet diversifies. Cost is a **proxy**: it uses run
wall-clock duration and excludes boot/terminate overhead and EBS.

Cardinality stays bounded — instance types are a small set.

### Dashboard (Lifetime / History section)
- Two cost stats: **Est. cost (all-time)** and **Avg cost / run**.
- **Est. cost** column added to the **By document type** table.
- New **By instance type (all-time)** table: Runs, Compute-time, Est. cost.

## Testing
- `fleet/metrics` cost + per-instance-type aggregation (incl. unpriced → $0,
  working rows excluded); `attach` compute_type round-trip in the registry/dynamo/CLI
  suites; exporter glue asserts the new labels and the cost series. 108 tests pass.
- Exporter smoke test confirms a 1h `t3.xlarge` run = $0.1664.

## Out of scope (still deferred)
- 30d/90d windowed variants; retry/stuck-run metrics; alerting; resolving real
  instance type for runs whose `attach` predates this change (they read `ec2` / $0).
