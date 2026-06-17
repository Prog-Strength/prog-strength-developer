"""Pure aggregation of run-history rows into Prometheus metric samples.

Stdlib only — no boto3, no prometheus_client — so it imports anywhere and
is exhaustively unit-testable. ``aggregate`` turns the immutable ``RUN#``
rows (scanned from DynamoDB as :class:`RunHistory`) into flat
:class:`MetricSample` tuples; ``bootstrap/ddb_exporter.py`` is the thin
shell that maps those onto gauges and serves them.

All metric names are prefixed ``developer_history_`` to namespace them
apart from the live ``developer_worker_*`` / ``developer_run_*`` metrics.
Labels are deliberately low-cardinality: ``doc_type`` (sow/dx/``all``) and
``outcome`` (success/error/timeout/working).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fleet.models import RunHistory

#: Terminal outcomes a finalized run can carry.
TERMINAL_OUTCOMES = ("success", "error", "timeout")
#: Every bucket the runs/PRs gauges enumerate — terminal plus the
#: not-yet-finalized "working" bucket.
OUTCOMES = TERMINAL_OUTCOMES + ("working",)
#: Reserved doc_type label for cross-cutting aggregates (avg/p90/max can't
#: be re-derived from per-doc_type series in PromQL, so we emit them here).
ALL = "all"

RUNS_TOTAL = "developer_history_runs_total"
PRS_OPENED_TOTAL = "developer_history_prs_opened_total"
COMPUTE_SECONDS_TOTAL = "developer_history_compute_seconds_total"
COMPUTE_COST_TOTAL = "developer_history_compute_cost_dollars_total"
DURATION_AVG = "developer_history_run_duration_seconds_avg"
DURATION_P90 = "developer_history_run_duration_seconds_p90"
DURATION_MAX = "developer_history_run_duration_seconds_max"

#: On-demand Linux $/hr by EC2 instance type, us-east-2. Hardcoded — the
#: fleet is small and single-region, so a static map beats a Pricing API
#: call. Extend as the fleet diversifies. The cost metric is a proxy:
#: run duration × this rate; an unpriced type contributes 0.
PRICE_USD_PER_HOUR = {
    "t3.large": 0.0832,
    "t3.xlarge": 0.1664,
    "t3.2xlarge": 0.3328,
}


def _hourly_rate(compute_type: str) -> float:
    """On-demand $/hr for a ``compute_type`` like ``ec2:t3.xlarge``. 0.0
    for a coarse ``ec2`` (type never resolved) or any unpriced type."""
    prefix, _, instance_type = compute_type.partition(":")
    if prefix != "ec2" or not instance_type:
        return 0.0
    return PRICE_USD_PER_HOUR.get(instance_type, 0.0)


@dataclass(frozen=True)
class MetricSample:
    """One labeled gauge reading: ``name{labels} = value``."""

    name: str
    labels: dict
    value: float


def _bucket(row: RunHistory) -> str:
    """Outcome bucket for a row — its terminal outcome, or ``working`` if
    it has not been finalized."""
    return row.outcome if row.outcome in TERMINAL_OUTCOMES else "working"


def _percentile(values: list[int], p: float) -> float:
    """Nearest-rank percentile. 0.0 for an empty set."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))
    return float(ordered[rank - 1])


def _duration_samples(doc_type: str, durations: list[int]) -> list[MetricSample]:
    avg = sum(durations) / len(durations) if durations else 0.0
    return [
        MetricSample(DURATION_AVG, {"doc_type": doc_type}, float(avg)),
        MetricSample(DURATION_P90, {"doc_type": doc_type}, _percentile(durations, 90)),
        MetricSample(DURATION_MAX, {"doc_type": doc_type}, float(max(durations)) if durations else 0.0),
    ]


def aggregate(rows: list[RunHistory]) -> list[MetricSample]:
    """Compute the v1 metric samples from every run-history row.

    Counts and PR sums are emitted for every (observed doc_type ×
    canonical outcome) pair — including zeros — so a bucket that empties
    (a ``working`` row finalizing) resets its gauge instead of leaving a
    stale series. Duration/compute stats consider terminal rows only; the
    ``doc_type="all"`` duration series is always emitted (0 when empty) so
    its panel never reads "No data".
    """
    samples: list[MetricSample] = []
    doc_types = sorted({r.doc_type for r in rows})

    for doc_type in doc_types:
        dt_rows = [r for r in rows if r.doc_type == doc_type]

        # PRs are summed by (doc_type, outcome) only.
        for outcome in OUTCOMES:
            matching = [r for r in dt_rows if _bucket(r) == outcome]
            prs = sum((r.prs_opened or 0) for r in matching)
            samples.append(
                MetricSample(PRS_OPENED_TOTAL, {"doc_type": doc_type, "outcome": outcome}, float(prs))
            )

        # Runs / compute-time / cost are additionally broken down by the
        # resolved instance type (compute_type).
        for compute_type in sorted({r.compute_type for r in dt_rows}):
            ct_rows = [r for r in dt_rows if r.compute_type == compute_type]
            base = {"doc_type": doc_type, "compute_type": compute_type}
            for outcome in OUTCOMES:
                n = sum(1 for r in ct_rows if _bucket(r) == outcome)
                samples.append(MetricSample(RUNS_TOTAL, {**base, "outcome": outcome}, float(n)))

            terminal = [r.duration_seconds for r in ct_rows if r.duration_seconds is not None]
            seconds = float(sum(terminal))
            samples.append(MetricSample(COMPUTE_SECONDS_TOTAL, base, seconds))
            samples.append(MetricSample(COMPUTE_COST_TOTAL, base, seconds / 3600 * _hourly_rate(compute_type)))

        # Duration stats stay per-doc_type (over all terminal rows).
        dt_terminal = [r.duration_seconds for r in dt_rows if r.duration_seconds is not None]
        samples.extend(_duration_samples(doc_type, dt_terminal))

    all_terminal = [r.duration_seconds for r in rows if r.duration_seconds is not None]
    samples.extend(_duration_samples(ALL, all_terminal))
    return samples
