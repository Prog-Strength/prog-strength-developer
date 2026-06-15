"""Contract tests for the fleet run registry.

These exercise the locking semantics that guarantee at most one active
worker per SOW. They run against the in-memory FakeRunRegistry; the
DynamoDB implementation is verified separately (test_fleet_dynamo.py)
against the same contract.

Time is passed in explicitly (`now`, epoch seconds) so the tests are
deterministic and never touch the wall clock.
"""

import pytest

from fleet.memory import FakeRunRegistry
from fleet.models import RunStatus
from fleet.registry import FleetError

TTL = 1000


def test_acquire_on_empty_sow_succeeds():
    reg = FakeRunRegistry()
    result = reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    assert result.acquired is True
    assert result.conflict is None
    rec = result.record
    assert rec.sow == "sows/foo.md"
    assert rec.status is RunStatus.WORKING
    assert rec.dispatch_id == "d1"
    assert rec.started_at == 100
    assert rec.expires_at == 1100  # now + ttl
    assert rec.instance_id is None


def test_acquire_when_active_worker_holds_sow_is_refused():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    result = reg.try_acquire("sows/foo.md", dispatch_id="d2", now=200, ttl_seconds=TTL)

    assert result.acquired is False
    assert result.record is None
    assert result.conflict is not None
    # The conflict surfaces the current holder so the caller can report it.
    assert result.conflict.dispatch_id == "d1"


def test_acquire_takes_over_a_terminal_run():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    reg.release("sows/foo.md", instance_id=None, outcome="success", now=150)

    result = reg.try_acquire("sows/foo.md", dispatch_id="d2", now=200, ttl_seconds=TTL)

    assert result.acquired is True
    assert result.record.dispatch_id == "d2"


def test_acquire_takes_over_an_expired_lock():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    # now (1200) is past the d1 lock's expires_at (1100): stale, reclaimable.
    result = reg.try_acquire("sows/foo.md", dispatch_id="d2", now=1200, ttl_seconds=TTL)

    assert result.acquired is True
    assert result.record.dispatch_id == "d2"


def test_attach_instance_sets_instance_on_own_reservation():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    rec = reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-123", now=110)

    assert rec.instance_id == "i-123"
    assert reg.get("sows/foo.md").instance_id == "i-123"


def test_attach_instance_rejects_a_foreign_dispatch():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    # A different dispatch must not be able to patch a lock it doesn't own.
    with pytest.raises(FleetError):
        reg.attach_instance("sows/foo.md", dispatch_id="dX", instance_id="i-evil", now=110)


def test_release_frees_the_sow_for_reacquire():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-123", now=110)

    released = reg.release("sows/foo.md", instance_id="i-123", outcome="success", now=150)

    assert released is True
    assert reg.get("sows/foo.md").status is RunStatus.DONE
    # SOW is now reacquirable.
    assert reg.try_acquire("sows/foo.md", dispatch_id="d2", now=160, ttl_seconds=TTL).acquired


def test_release_from_a_superseded_worker_does_not_free_the_new_owner():
    """A worker whose lock expired and was taken over must not release the
    new owner's lock when it finally finishes."""
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-old", now=110)
    # d1 expires; d2 takes over with a different instance.
    reg.try_acquire("sows/foo.md", dispatch_id="d2", now=1200, ttl_seconds=TTL)
    reg.attach_instance("sows/foo.md", dispatch_id="d2", instance_id="i-new", now=1210)

    # The old worker now tries to release — must be a no-op.
    released = reg.release("sows/foo.md", instance_id="i-old", outcome="error", now=1300)

    assert released is False
    rec = reg.get("sows/foo.md")
    assert rec.status is RunStatus.WORKING
    assert rec.instance_id == "i-new"


def test_force_release_clears_any_lock():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    reg.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-stuck", now=110)

    released = reg.release(
        "sows/foo.md", instance_id="anything", outcome="error", now=200, force=True
    )

    assert released is True
    assert reg.get("sows/foo.md").status is RunStatus.ERROR


def test_list_active_returns_only_unexpired_working_runs():
    reg = FakeRunRegistry()
    reg.try_acquire("sows/a.md", dispatch_id="da", now=100, ttl_seconds=TTL)  # expires 1100
    reg.try_acquire("sows/b.md", dispatch_id="db", now=100, ttl_seconds=TTL)
    reg.release("sows/b.md", instance_id=None, outcome="success", now=150)  # terminal
    reg.try_acquire("sows/c.md", dispatch_id="dc", now=100, ttl_seconds=10)  # expires 110

    active = reg.list_active(now=500)  # a still valid; c expired; b terminal

    sows = {r.sow for r in active}
    assert sows == {"sows/a.md"}


def test_get_unknown_sow_returns_none():
    reg = FakeRunRegistry()
    assert reg.get("sows/missing.md") is None


def test_outcome_maps_to_terminal_status():
    assert RunStatus.from_outcome("success") is RunStatus.DONE
    assert RunStatus.from_outcome("error") is RunStatus.ERROR
    assert RunStatus.from_outcome("timeout") is RunStatus.TIMEOUT
