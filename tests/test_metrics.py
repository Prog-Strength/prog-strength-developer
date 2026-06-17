"""Tests for the pure run-history aggregation in fleet.metrics.

aggregate() turns a list of RunHistory rows (the RUN# items scanned from
DynamoDB) into flat metric samples the exporter maps onto Prometheus
gauges. No AWS, no prometheus_client — just the math.
"""

import pytest

from fleet import metrics
from fleet.metrics import aggregate
from fleet.models import RunHistory, RunStatus


def _val(samples, name, **labels):
    for s in samples:
        if s.name == name and s.labels == labels:
            return s.value
    return None


def _terminal(doc_type, outcome, dur, prs, started=100, compute_type="ec2"):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.from_outcome(outcome),
        started_at=started,
        updated_at=started + dur,
        compute_type=compute_type,
        outcome=outcome,
        finished_at=started + dur,
        duration_seconds=dur,
        prs_opened=prs,
    )


def _working(doc_type, started=100, compute_type="ec2"):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.WORKING,
        started_at=started,
        updated_at=started,
        compute_type=compute_type,
    )


def test_runs_counted_by_doc_type_outcome_and_compute_type():
    rows = [
        _terminal("sow", "success", 10, 1),
        _terminal("sow", "success", 20, 1),
        _terminal("sow", "error", 30, 0),
        _terminal("dx", "success", 40, 1),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", outcome="success") == 2
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", outcome="error") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="dx", compute_type="ec2", outcome="success") == 1


def test_runs_split_by_instance_type():
    rows = [
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.xlarge"),
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.2xlarge"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge", outcome="success") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2:t3.2xlarge", outcome="success") == 1


def test_canonical_outcomes_emitted_even_when_zero():
    s = aggregate([_terminal("sow", "success", 10, 1)])
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", outcome="timeout") == 0
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", outcome="working") == 0


def test_working_rows_bucketed_and_excluded_from_duration():
    rows = [_working("sow"), _terminal("sow", "success", 50, 2)]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", compute_type="ec2", outcome="working") == 1
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2") == 50
    assert _val(s, metrics.DURATION_AVG, doc_type="sow") == 50


def test_prs_summed_by_doc_type_and_outcome():
    rows = [
        _terminal("sow", "success", 10, 2),
        _terminal("sow", "success", 10, 3),
        _working("sow"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.PRS_OPENED_TOTAL, doc_type="sow", outcome="success") == 5
    assert _val(s, metrics.PRS_OPENED_TOTAL, doc_type="sow", outcome="working") == 0


def test_compute_seconds_summed_by_doc_type_and_compute_type():
    rows = [
        _terminal("sow", "success", 10, 0, compute_type="ec2:t3.xlarge"),
        _terminal("sow", "error", 25, 0, compute_type="ec2:t3.xlarge"),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge") == 35


def test_duration_avg_p90_max():
    rows = [_terminal("sow", "success", d, 0) for d in range(10, 101, 10)]  # 10..100
    s = aggregate(rows)
    assert _val(s, metrics.DURATION_AVG, doc_type="sow") == 55
    assert _val(s, metrics.DURATION_MAX, doc_type="sow") == 100
    assert _val(s, metrics.DURATION_P90, doc_type="sow") == 90  # nearest-rank


def test_all_bucket_aggregates_durations_across_doc_types():
    rows = [_terminal("sow", "success", 10, 0), _terminal("dx", "success", 30, 0)]
    s = aggregate(rows)
    assert _val(s, metrics.DURATION_AVG, doc_type="all") == 20
    assert _val(s, metrics.DURATION_MAX, doc_type="all") == 30


def test_cost_is_duration_times_hardcoded_instance_rate():
    # t3.xlarge us-east-2 on-demand = $0.1664/hr; one full hour = $0.1664.
    rows = [_terminal("sow", "success", 3600, 0, compute_type="ec2:t3.xlarge")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge") == pytest.approx(0.1664)


def test_cost_is_zero_for_unpriced_compute_type():
    # A coarse "ec2" (attach never resolved the type) has no rate → no cost.
    rows = [_terminal("sow", "success", 3600, 0, compute_type="ec2")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2") == 0


def test_cost_excludes_working_rows():
    rows = [_working("sow", compute_type="ec2:t3.xlarge")]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_COST_TOTAL, doc_type="sow", compute_type="ec2:t3.xlarge") == 0


def test_empty_input_emits_zero_all_durations_only():
    s = aggregate([])
    assert _val(s, metrics.DURATION_AVG, doc_type="all") == 0
    assert _val(s, metrics.DURATION_P90, doc_type="all") == 0
    assert _val(s, metrics.DURATION_MAX, doc_type="all") == 0
    assert not [x for x in s if x.name == metrics.RUNS_TOTAL]
