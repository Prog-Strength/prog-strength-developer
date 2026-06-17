"""Tests for the pure run-history aggregation in fleet.metrics.

aggregate() turns a list of RunHistory rows (the RUN# items scanned from
DynamoDB) into flat metric samples the exporter maps onto Prometheus
gauges. No AWS, no prometheus_client — just the math.
"""

from fleet import metrics
from fleet.metrics import aggregate
from fleet.models import RunHistory, RunStatus


def _val(samples, name, **labels):
    for s in samples:
        if s.name == name and s.labels == labels:
            return s.value
    return None


def _terminal(doc_type, outcome, dur, prs, started=100):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.from_outcome(outcome),
        started_at=started,
        updated_at=started + dur,
        compute_type="ec2",
        outcome=outcome,
        finished_at=started + dur,
        duration_seconds=dur,
        prs_opened=prs,
    )


def _working(doc_type, started=100):
    return RunHistory(
        sow="t",
        dispatch_id="d",
        doc_type=doc_type,
        status=RunStatus.WORKING,
        started_at=started,
        updated_at=started,
        compute_type="ec2",
    )


def test_runs_counted_by_doc_type_and_outcome():
    rows = [
        _terminal("sow", "success", 10, 1),
        _terminal("sow", "success", 20, 1),
        _terminal("sow", "error", 30, 0),
        _terminal("dx", "success", 40, 1),
    ]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", outcome="success") == 2
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", outcome="error") == 1
    assert _val(s, metrics.RUNS_TOTAL, doc_type="dx", outcome="success") == 1


def test_canonical_outcomes_emitted_even_when_zero():
    # A doc_type that has appeared must emit every outcome bucket so a
    # bucket that empties (e.g. working->finalized) is set back to 0 rather
    # than leaving a stale gauge series.
    s = aggregate([_terminal("sow", "success", 10, 1)])
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", outcome="timeout") == 0
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", outcome="working") == 0


def test_working_rows_bucketed_and_excluded_from_duration():
    rows = [_working("sow"), _terminal("sow", "success", 50, 2)]
    s = aggregate(rows)
    assert _val(s, metrics.RUNS_TOTAL, doc_type="sow", outcome="working") == 1
    # Duration/compute see only the terminal row.
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow") == 50
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


def test_compute_seconds_is_sum_of_terminal_durations():
    rows = [_terminal("sow", "success", 10, 0), _terminal("sow", "error", 25, 0)]
    s = aggregate(rows)
    assert _val(s, metrics.COMPUTE_SECONDS_TOTAL, doc_type="sow") == 35


def test_duration_avg_p90_max():
    rows = [_terminal("sow", "success", d, 0) for d in range(10, 101, 10)]  # 10..100
    s = aggregate(rows)
    assert _val(s, metrics.DURATION_AVG, doc_type="sow") == 55
    assert _val(s, metrics.DURATION_MAX, doc_type="sow") == 100
    # nearest-rank p90 of 10 values: ceil(0.9*10)=9 -> 9th smallest = 90
    assert _val(s, metrics.DURATION_P90, doc_type="sow") == 90


def test_all_bucket_aggregates_durations_across_doc_types():
    rows = [_terminal("sow", "success", 10, 0), _terminal("dx", "success", 30, 0)]
    s = aggregate(rows)
    assert _val(s, metrics.DURATION_AVG, doc_type="all") == 20
    assert _val(s, metrics.DURATION_MAX, doc_type="all") == 30


def test_empty_input_emits_zero_all_durations_only():
    s = aggregate([])
    assert _val(s, metrics.DURATION_AVG, doc_type="all") == 0
    assert _val(s, metrics.DURATION_P90, doc_type="all") == 0
    assert _val(s, metrics.DURATION_MAX, doc_type="all") == 0
    # No per-doc_type or count series when there is no data.
    assert not [x for x in s if x.name == metrics.RUNS_TOTAL]
