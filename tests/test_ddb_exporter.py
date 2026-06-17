"""Tests for the DynamoDB run-history exporter glue.

The aggregation math is tested in test_metrics.py and the scan in
test_fleet_dynamo.py. Here we prove the thin shell: that a refresh maps
aggregated samples onto Prometheus gauges, records health, and survives a
scan failure without crashing. A FakeRunRegistry stands in for DynamoDB.
"""

import pytest

from bootstrap import ddb_exporter
from fleet.memory import FakeRunRegistry

TTL = 1000


def _registry_with_runs():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/a.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    reg.attach_instance(
        "sows/a.md", dispatch_id="d1", instance_id="i-1", now=110, compute_type="ec2:t3.xlarge"
    )
    reg.release("sows/a.md", instance_id="i-1", outcome="success", now=460, prs_opened=2)
    reg.try_acquire("dx/b.md", dispatch_id="d2", now=500, ttl_seconds=TTL)  # in flight, coarse "ec2"
    return reg


def test_refresh_publishes_aggregated_gauges():
    prom, gauges = ddb_exporter.build_metrics()
    ddb_exporter.refresh(gauges, _registry_with_runs(), now=999)

    sv = prom.get_sample_value
    assert sv("developer_history_runs_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge", "outcome": "success"}) == 1
    assert sv("developer_history_runs_total", {"doc_type": "dx", "compute_type": "ec2", "outcome": "working"}) == 1
    assert sv("developer_history_prs_opened_total", {"doc_type": "sow", "outcome": "success"}) == 2
    assert sv("developer_history_compute_seconds_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge"}) == 360
    assert sv("developer_history_run_duration_seconds_max", {"doc_type": "all"}) == 360
    # cost = 360s / 3600 * $0.1664/hr
    assert sv("developer_history_compute_cost_dollars_total", {"doc_type": "sow", "compute_type": "ec2:t3.xlarge"}) == pytest.approx(0.01664)


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
