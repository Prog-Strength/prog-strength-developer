"""Tests for the DynamoDB run-history exporter glue.

The aggregation math is tested in test_metrics.py and the scan in
test_fleet_dynamo.py. Here we prove the thin shell: that a refresh maps
aggregated samples onto Prometheus gauges, records health, and survives a
scan failure without crashing. A FakeRunRegistry stands in for DynamoDB.
"""

import pytest

from bootstrap import ddb_exporter
from fleet import metrics
from fleet.memory import FakeRunRegistry
from fleet.models import RunHistory, RunStatus

TTL = 1000


def _registry_with_runs():
    reg = FakeRunRegistry()
    reg.try_acquire(
        "sows/a.md", dispatch_id="d1", now=100, ttl_seconds=TTL, model="claude-fable-5"
    )
    reg.attach_instance(
        "sows/a.md", dispatch_id="d1", instance_id="i-1", now=110, compute_type="ec2:t3.xlarge"
    )
    reg.release(
        "sows/a.md", instance_id="i-1", outcome="success", now=460, prs_opened=2,
        model="claude-fable-5",
    )
    # In flight: coarse "ec2", and a different model.
    reg.try_acquire(
        "dx/b.md", dispatch_id="d2", now=500, ttl_seconds=TTL, model="claude-opus-5"
    )
    return reg


def test_refresh_publishes_aggregated_gauges():
    prom, gauges = ddb_exporter.build_metrics()
    ddb_exporter.refresh(gauges, _registry_with_runs(), now=999)

    sv = prom.get_sample_value
    assert sv("developer_history_runs_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5", "outcome": "success"}) == 1
    assert sv("developer_history_runs_total", {"doc_type": "dx", "compute_type": "ec2", "model": "claude-opus-5", "outcome": "working"}) == 1
    assert sv("developer_history_prs_opened_total", {"doc_type": "sow", "outcome": "success"}) == 2
    assert sv("developer_history_compute_seconds_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5"}) == 360
    assert sv("developer_history_run_duration_seconds_max", {"doc_type": "all"}) == 360
    # cost = 360s / 3600 * $0.1664/hr
    assert sv("developer_history_compute_cost_dollars_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "model": "claude-fable-5"}) == pytest.approx(0.01664)


def test_refresh_records_scan_health():
    prom, gauges = ddb_exporter.build_metrics()
    ddb_exporter.refresh(gauges, _registry_with_runs(), now=999)

    assert prom.get_sample_value("developer_history_last_scan_timestamp_seconds") == 999
    assert prom.get_sample_value("developer_history_scanned_rows") == 2


def test_refresh_survives_scan_error_without_crashing():
    class Boom(FakeRunRegistry):
        def scan_history(self):
            raise RuntimeError("dynamo unavailable")

    prom, gauges = ddb_exporter.build_metrics()
    # Must not raise.
    ddb_exporter.refresh(gauges, Boom(), now=42)

    assert prom.get_sample_value("developer_history_scan_errors_total") == 1
    # A failed scan leaves the health timestamp untouched (stays 0).
    assert prom.get_sample_value("developer_history_last_scan_timestamp_seconds") == 0


def test_labeled_gauges_match_every_shape_aggregate_emits():
    """Guards the ddb_exporter <-> fleet.metrics label contract.

    ``_LABELED`` restates the label sets that ``fleet.metrics.aggregate``
    builds inline; the two are separate sources of truth that must agree
    exactly, or ``gauges[name].labels(**labels)`` raises ``ValueError:
    Incorrect label names`` at runtime on the manager host. This drifted
    once already — a ``model`` label landed in ``aggregate()`` a commit
    before this module caught up — and was caught only because two
    unrelated tests happened to name the affected metrics. Nothing here
    asserts on DURATION_P90 or DURATION_AVG, so a label added to either
    would ship silently.

    This walks every metric ``aggregate()`` can emit over a fixture rich
    enough to hit every label combination, and fails naming the offending
    metric and both label sets.
    """
    rows = [
        RunHistory(
            sow="a", dispatch_id="d1", doc_type="sow", status=RunStatus.DONE,
            started_at=0, updated_at=10, compute_type="ec2:t3.xlarge",
            model="claude-fable-5", outcome="success", finished_at=10,
            duration_seconds=10, prs_opened=1,
        ),
        RunHistory(
            sow="b", dispatch_id="d2", doc_type="sow", status=RunStatus.ERROR,
            started_at=0, updated_at=20, compute_type="ec2:t3.2xlarge",
            model="claude-opus-5", outcome="error", finished_at=20,
            duration_seconds=20, prs_opened=0,
        ),
        RunHistory(
            sow="c", dispatch_id="d3", doc_type="dx", status=RunStatus.WORKING,
            started_at=0, updated_at=0, compute_type="ec2",
            model="claude-sonnet-5",
        ),
        # Legacy row predating model capture.
        RunHistory(
            sow="d", dispatch_id="d4", doc_type="dx", status=RunStatus.TIMEOUT,
            started_at=0, updated_at=30, compute_type="ec2:t3.large",
            model="unknown", outcome="timeout", finished_at=30,
            duration_seconds=30, prs_opened=0,
        ),
    ]

    _, gauges = ddb_exporter.build_metrics()

    for sample in metrics.aggregate(rows):
        assert sample.name in ddb_exporter._LABELED, (
            f"{sample.name!r} has no declared gauge in ddb_exporter._LABELED"
        )
        declared = set(ddb_exporter._LABELED[sample.name])
        emitted = set(sample.labels)
        assert emitted == declared, (
            f"{sample.name}: aggregate() emitted labels {sorted(emitted)} "
            f"but ddb_exporter._LABELED declares {sorted(declared)} "
            f"(symmetric difference: {sorted(emitted ^ declared)})"
        )
        # The real runtime failure mode: prometheus_client raises
        # ValueError here if the label sets don't match exactly.
        gauges[sample.name].labels(**sample.labels).set(sample.value)
