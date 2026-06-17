"""Tests for the fleet CLI — the adapter the dispatch workflow and the
worker shell out to. Exit codes are part of the contract: the workflow
branches on them.

Each test injects a FakeRunRegistry and a fixed ``now`` so nothing
touches AWS or the wall clock.
"""

import json

from fleet import cli
from fleet.memory import FakeRunRegistry
from fleet.models import RunStatus

OK = cli.EXIT_OK
ERROR = cli.EXIT_ERROR
CONFLICT = cli.EXIT_CONFLICT


def run(argv, reg, now=100):
    return cli.run(argv, registry=reg, now=now)


def test_acquire_succeeds_and_records_the_lock():
    reg = FakeRunRegistry()
    code = run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    assert code == OK
    assert reg.get("sows/foo.md").status is RunStatus.WORKING


def test_acquire_conflict_returns_distinct_exit_code(capsys):
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg, now=100)
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-aaa", now=101)

    code = run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d2"], reg, now=200)

    assert code == CONFLICT
    # The operator-facing message names the worker already on the SOW.
    assert "i-aaa" in capsys.readouterr().out


def test_acquire_json_emits_dispatch_id(capsys):
    reg = FakeRunRegistry()
    code = run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--json"], reg)
    assert code == OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["acquired"] is True
    assert payload["dispatch_id"] == "d1"


def test_attach_records_instance_on_own_lock():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    code = run(
        ["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-123"],
        reg,
    )
    assert code == OK
    assert reg.get("sows/foo.md").instance_id == "i-123"


def test_attach_foreign_dispatch_errors():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    code = run(
        ["attach", "--sow", "sows/foo.md", "--dispatch-id", "dX", "--instance-id", "i-evil"],
        reg,
    )
    assert code == ERROR


def test_release_matching_instance_frees_sow():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-1"], reg)

    code = run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "i-1", "--outcome", "success"],
        reg,
    )
    assert code == OK
    assert reg.get("sows/foo.md").status is RunStatus.DONE


def test_release_is_best_effort_on_mismatch(capsys):
    """A superseded worker calling release must not fail the caller — it
    exits OK but reports that it skipped (the lock belongs to someone
    else now)."""
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-new"], reg)

    code = run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "i-old", "--outcome", "error"],
        reg,
    )
    assert code == OK
    assert "skip" in capsys.readouterr().out.lower()
    assert reg.get("sows/foo.md").status is RunStatus.WORKING  # untouched


def test_force_release_overrides_mismatch():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-1"], reg)
    code = run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "x", "--outcome", "error", "--force"],
        reg,
    )
    assert code == OK
    assert reg.get("sows/foo.md").status is RunStatus.ERROR


def test_list_shows_active_runs(capsys):
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/a.md", "--dispatch-id", "da"], reg)
    code = run(["list"], reg)
    assert code == OK
    assert "sows/a.md" in capsys.readouterr().out


def test_acquire_records_doc_type_from_path():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "dx/cards.md", "--dispatch-id", "d1"], reg)
    assert reg.list_history("dx/cards.md")[0].doc_type == "dx"


def test_release_threads_prs_opened_into_history():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg, now=100)
    run(["attach", "--sow", "sows/foo.md", "--dispatch-id", "d1", "--instance-id", "i-1"], reg)
    code = run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "i-1",
         "--outcome", "success", "--prs-opened", "4"],
        reg,
        now=500,
    )
    assert code == OK
    row = reg.list_history("sows/foo.md")[0]
    assert row.prs_opened == 4
    assert row.duration_seconds == 400


def test_release_defaults_prs_opened_to_zero():
    reg = FakeRunRegistry()
    run(["acquire", "--sow", "sows/foo.md", "--dispatch-id", "d1"], reg)
    run(
        ["release", "--sow", "sows/foo.md", "--instance-id", "none",
         "--outcome", "error", "--force"],
        reg,
    )
    assert reg.list_history("sows/foo.md")[0].prs_opened == 0
