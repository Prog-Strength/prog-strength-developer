"""Long-lived Prometheus exporter for the DynamoDB run-history table.

Runs on the manager (a docker-compose service). Every refresh interval it
scans the immutable ``RUN#`` rows of ``prog-strength-developer-runs``,
aggregates them via :func:`fleet.metrics.aggregate`, and publishes the
result as gauges on ``:9102`` for Prometheus to scrape. The scan cadence
(default 60s) is decoupled from Prometheus's scrape interval, so a full
table scan happens at most once a minute regardless of scrape rate.

Mirrors ``bootstrap/worker_exporter.py``: the aggregation math and the
DynamoDB scan live in the testable ``fleet`` package; this module is the
thin shell that maps samples onto ``prometheus_client`` metrics. The
``build_metrics`` / ``refresh`` split keeps the glue unit-testable against
a FakeRunRegistry without AWS.
"""

from __future__ import annotations

import logging
import os
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

from fleet import metrics
from fleet.config import Config
from fleet.registry import RunRegistry

log = logging.getLogger("ddb_exporter")

DEFAULT_PORT = 9102
DEFAULT_REFRESH_SECONDS = 60

#: Aggregate gauges keyed by metric name → label set, matching the samples
#: produced by fleet.metrics.aggregate.
_LABELED = {
    metrics.RUNS_TOTAL: ["doc_type", "compute_type", "outcome"],
    metrics.PRS_OPENED_TOTAL: ["doc_type", "outcome"],
    metrics.COMPUTE_SECONDS_TOTAL: ["doc_type", "compute_type"],
    metrics.COMPUTE_COST_TOTAL: ["doc_type", "compute_type"],
    metrics.DURATION_AVG: ["doc_type"],
    metrics.DURATION_P90: ["doc_type"],
    metrics.DURATION_MAX: ["doc_type"],
}

_HELP = {
    metrics.RUNS_TOTAL: "Run-history rows by doc_type, instance type, and outcome (outcome=working is in-flight).",
    metrics.PRS_OPENED_TOTAL: "PRs opened, summed by doc_type and outcome.",
    metrics.COMPUTE_SECONDS_TOTAL: "Cumulative worker compute-time (sum of run durations) by doc_type and instance type.",
    metrics.COMPUTE_COST_TOTAL: "Estimated on-demand cost (USD): run duration x hardcoded us-east-2 hourly rate, by doc_type and instance type.",
    metrics.DURATION_AVG: "Mean run duration over terminal runs by doc_type (all = across types).",
    metrics.DURATION_P90: "p90 run duration over terminal runs by doc_type (all = across types).",
    metrics.DURATION_MAX: "Max run duration over terminal runs by doc_type (all = across types).",
}


def build_metrics() -> tuple[CollectorRegistry, dict]:
    """Construct a fresh registry and the metric handles. Returns
    ``(registry, gauges)`` where ``gauges`` is keyed by metric name for the
    aggregate gauges plus ``last_scan`` / ``scanned_rows`` / ``scan_errors``
    for the exporter's own health."""
    registry = CollectorRegistry()
    gauges: dict = {
        name: Gauge(name, _HELP[name], labels, registry=registry)
        for name, labels in _LABELED.items()
    }
    gauges["last_scan"] = Gauge(
        "developer_history_last_scan_timestamp_seconds",
        "Epoch seconds of the last successful table scan.",
        registry=registry,
    )
    gauges["scanned_rows"] = Gauge(
        "developer_history_scanned_rows",
        "Number of run-history rows seen in the last successful scan.",
        registry=registry,
    )
    gauges["scan_errors"] = Counter(
        "developer_history_scan_errors",
        "Count of failed table scans (the exporter keeps serving the last good values).",
        registry=registry,
    )
    return registry, gauges


def refresh(gauges: dict, run_registry: RunRegistry, now: int) -> None:
    """Scan the history table, aggregate, and publish onto ``gauges``.

    A scan failure is swallowed: bump the error counter, log, and leave the
    last good gauge values in place so a transient DynamoDB blip never takes
    the exporter down or zeroes the dashboard."""
    try:
        rows = run_registry.scan_history()
    except Exception:  # noqa: BLE001 — resilience is the whole point here
        gauges["scan_errors"].inc()
        log.exception("run-history scan failed; serving last good values")
        return

    for sample in metrics.aggregate(rows):
        gauges[sample.name].labels(**sample.labels).set(sample.value)
    gauges["scanned_rows"].set(len(rows))
    gauges["last_scan"].set(now)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from fleet.dynamo import DynamoRunRegistry  # lazy: keeps boto3 out of tests

    cfg = Config.from_env()
    port = int(os.environ.get("DDB_EXPORTER_PORT", DEFAULT_PORT))
    interval = int(os.environ.get("DDB_EXPORTER_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS))

    registry, gauges = build_metrics()
    run_registry = DynamoRunRegistry(table_name=cfg.table_name, region=cfg.region)
    start_http_server(port, registry=registry)
    log.info("ddb_exporter serving on :%d, scanning %s every %ds", port, cfg.table_name, interval)

    while True:
        refresh(gauges, run_registry, now=int(time.time()))
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
