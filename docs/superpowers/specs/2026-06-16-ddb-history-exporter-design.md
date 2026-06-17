# DynamoDB run-history Prometheus exporter

**Date:** 2026-06-16
**Status:** approved, ready for implementation plan
**Depends on:** the lock + run-history table ([2026-06-16-run-history-table-design.md](2026-06-16-run-history-table-design.md), already implemented)

## Problem & goal

The `prog-strength-developer-runs` table now keeps an immutable `RUN#` row per dispatch
(status, outcome, duration, PRs, doc_type, compute_type) — a durable, all-time record.
Nothing reads it yet. The live worker metrics (Pushgateway → Prometheus) are per-run and
only retained ~15 days, so they cannot answer lifetime questions.

Add a long-lived Prometheus exporter on the manager that periodically scans the `RUN#`
rows, computes aggregates, and exposes them as gauges. Prometheus scrapes it; new Grafana
panels show the aggregates **alongside** the existing live developer metrics on
`developer-platform.json`.

**v1 metric families (chosen):** volume & outcome, duration & compute-cost, PR output.
(Retry/stuck-run signals deferred — live in-flight health is already covered.)

## Architecture — testable core, thin IO shell

```
fleet/dynamo.py            scan_history() — paginated Scan, begins_with(sk,"RUN#") → list[RunHistory]
fleet/registry.py          + abstract scan_history();  fleet/memory.py implements it
fleet/metrics.py           NEW, pure stdlib — aggregate(rows, now) → list[Sample(name, labels, value)]
bootstrap/ddb_exporter.py  NEW — 60s refresh loop: scan → aggregate → set gauges; serves :9102
monitoring/ddb_exporter/Dockerfile   python:slim + fleet pkg + the exporter
```

All aggregation math is a **pure function** in `fleet/metrics.py` (`list[RunHistory] →
samples`), unit-tested with hand-built rows and zero AWS. `scan_history()` is moto-tested.
`bootstrap/ddb_exporter.py` is a thin shell mapping samples onto `prometheus_client`
gauges and serving them — mirroring `bootstrap/worker_exporter.py`. The exporter depends on
the `fleet` package, so all DynamoDB knowledge stays in one place. The exporter lives in
`bootstrap/` (not `monitoring/`) so it is importable by tests the same way `worker_exporter.py`
is; its Dockerfile COPYs `bootstrap/ddb_exporter.py` + `fleet/` into the image.

## Metric set

All names are prefixed `developer_history_*` — namespaced apart from the live
`developer_worker_*` / `developer_claude_*` / `developer_run_*` (Pushgateway) metrics.
**Cardinality is bounded by design:** labels are only `doc_type` (sow/dx/`all`) and
`outcome` (success/error/timeout/working).

| Metric | Labels | Meaning |
|---|---|---|
| `developer_history_runs_total` | `doc_type, outcome` | run counts; `outcome="working"` = not-yet-finalized |
| `developer_history_prs_opened_total` | `doc_type, outcome` | Σ `prs_opened` |
| `developer_history_compute_seconds_total` | `doc_type` | Σ `duration_seconds` (cumulative compute = cost proxy) |
| `developer_history_run_duration_seconds_avg` | `doc_type` (+`all`) | avg over terminal rows |
| `developer_history_run_duration_seconds_p90` | `doc_type` (+`all`) | p90, nearest-rank, computed in-exporter |
| `developer_history_run_duration_seconds_max` | `doc_type` (+`all`) | max over terminal rows |
| `developer_history_last_scan_timestamp_seconds` | — | health: epoch of last successful scan |
| `developer_history_scanned_rows` | — | health: rows seen in last scan |
| `developer_history_scan_errors_total` | — | health: counter of failed scans |

All metrics are **gauges** (the exporter re-derives from the table each refresh) except
`scan_errors_total` (a counter). **Ratios are computed in Grafana PromQL, not the exporter:**

- all-time success rate = `sum(developer_history_runs_total{outcome="success"}) / sum(developer_history_runs_total{outcome=~"success|error|timeout"})`
- PRs per successful run = `sum(developer_history_prs_opened_total{outcome="success"}) / sum(developer_history_runs_total{outcome="success"})`

`avg/p90/max` cannot be re-derived from per-`doc_type` series in PromQL, so the exporter
also emits a `doc_type="all"` series for those three. **v1 is all-time only**; a rolling
30d window is a clean follow-on (new metric names, same scan).

### Aggregation rules (pure function)

- `runs_total` / `prs_opened_total` count every row; rows with no terminal `outcome`
  (status `working`) are bucketed under `outcome="working"`. A `working` row's
  `prs_opened` is treated as 0.
- Duration stats (`avg`/`p90`/`max`) and `compute_seconds_total` are over **terminal rows
  only** (those with `duration_seconds` set); `working` rows are excluded.
- Empty input (or no terminal rows) → the relevant gauges read 0.

## Deployment & infra

- **docker-compose service** `ddb_exporter` in `monitoring/docker-compose.yml`, built from
  `monitoring/ddb_exporter/Dockerfile` with **build context = repo root** so it can COPY
  `fleet/`. No host port mapping — Prometheus scrapes it over the compose network at
  `ddb_exporter:9102` (like `pushgateway`). `restart: unless-stopped`. Env:
  `AWS_REGION=us-east-2`, `DDB_EXPORTER_REFRESH_SECONDS=60`, `DDB_EXPORTER_PORT=9102`.
  AWS creds arrive via IMDS (manager already sets `http_put_response_hop_limit = 2` so
  containers can reach IMDS).
- **prometheus.yml** — new static scrape job:
  ```yaml
  - job_name: ddb_runs_exporter
    static_configs:
      - targets: ["ddb_exporter:9102"]
  ```
- **terraform/manager.tf** — add a statement to the manager inline policy granting
  `dynamodb:Scan` on `aws_dynamodb_table.runs.arn`. Applied via the normal Terraform
  plan/apply workflow (NOT `deploy-manager`). Order-independent: if the exporter runs
  before the grant lands, it bumps `scan_errors_total` and self-heals once IAM applies.
- **Grafana** — new **"Lifetime / history"** row appended to
  `monitoring/grafana/dashboards/developer-platform.json` (file-provisioned, read-only UI):
  stat panels for *total runs*, *all-time success rate*, *cumulative compute-time* (sec→h),
  *total PRs opened*, *p90 duration* (`doc_type="all"`); plus a small per-`doc_type` table
  (runs, success rate, avg duration, PRs/run).

### GitHub workflow changes (`deploy-manager.yml` only)

Two edits, no new workflow:

1. **`docker compose up -d` → `docker compose up -d --build`.** Our service is a `build:`
   service; `docker compose pull` skips build-only services and plain `up -d` reuses the
   cached image, so `--build` is required to pick up changed exporter/`fleet` code.
2. **Extend the `paths:` trigger** to add `fleet/**` and `bootstrap/ddb_exporter.py`. The
   existing `monitoring/**` already covers the compose file, Dockerfile, `prometheus.yml`,
   and dashboard; the exporter's Python and its `fleet` dependency live outside
   `monitoring/`, so a code-only change must also fire the deploy. `workflow_dispatch`
   remains as a manual fallback.

**Persistence across releases/reboots needs nothing more:** `restart: unless-stopped` keeps
the container alive across crashes/reboots, the manager's boot userdata already brings the
compose stack up, and every deploy's `up -d --build` recreates the service on change. No
systemd unit, no cron.

## Error handling

- A scan failure (throttle / IAM / network) is caught: increment `scan_errors_total`, log,
  keep serving the last-good gauge values, never crash. A stalled exporter manifests as
  `developer_history_last_scan_timestamp_seconds` going flat (alertable later).
- The exporter serves metrics immediately on start; the first scan populates real values
  within one refresh interval.

## Testing

- **`fleet/metrics.py aggregate()`** — pure unit tests: counts by doc_type/outcome,
  `working`-row bucketing, duration `avg`/`p90`/`max` over terminal-only rows, p90
  nearest-rank on small N, `compute_seconds_total` sum, `doc_type="all"` series, empty input
  → zeros.
- **`scan_history()`** — moto test: a table holding both `LOCK` and `RUN#` items returns
  only the `RUN#` rows, correctly deserialized to `RunHistory`.
- **exporter glue** — given sample rows, gauges read back expected values via
  `registry.get_sample_value`; a scan that raises bumps `scan_errors_total` and leaves the
  last-good gauges intact without crashing. Mirrors `tests/test_worker_exporter.py`'s
  importable-function approach.

## Out of scope (follow-on)

- Rolling 30d/90d windowed variants of the volume/rate metrics.
- Retry-rate and stuck/abandoned-run health metrics (the deferred metric family).
- Alerting rules on `scan_errors_total` / scan staleness.
