"""DynamoRunRegistry against a moto-mocked DynamoDB.

The in-memory FakeRunRegistry already proves the locking *contract*
(test_fleet_registry.py). These tests prove the DynamoDB implementation
honors that same contract — in particular that the conditional write
actually refuses a second concurrent acquire — so the two stay in lockstep.
"""

import boto3
import pytest
from moto import mock_aws

from fleet.dynamo import DynamoRunRegistry
from fleet.models import RunStatus
from fleet.registry import FleetError

TABLE = "test-developer-runs"
REGION = "us-east-2"
TTL = 1000


@pytest.fixture
def registry():
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[
                {"AttributeName": "sow", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "sow", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield DynamoRunRegistry(table_name=TABLE, region=REGION)


def test_acquire_then_conflict(registry):
    first = registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    assert first.acquired is True

    # A second acquire while d1 holds the (unexpired) lock must be refused
    # by the conditional write, not a read-then-write race.
    second = registry.try_acquire("sows/foo.md", dispatch_id="d2", now=200, ttl_seconds=TTL)
    assert second.acquired is False
    assert second.conflict is not None
    assert second.conflict.dispatch_id == "d1"


def test_takeover_after_expiry(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    later = registry.try_acquire("sows/foo.md", dispatch_id="d2", now=2000, ttl_seconds=TTL)
    assert later.acquired is True
    assert registry.get("sows/foo.md").dispatch_id == "d2"


def test_attach_and_release_roundtrip(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    registry.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-123", now=110)
    assert registry.get("sows/foo.md").instance_id == "i-123"

    assert registry.release("sows/foo.md", instance_id="i-123", outcome="success", now=150)
    assert registry.get("sows/foo.md").status is RunStatus.DONE


def test_attach_foreign_dispatch_raises(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    with pytest.raises(FleetError):
        registry.attach_instance("sows/foo.md", dispatch_id="dX", instance_id="i-x", now=110)


def test_release_mismatch_is_noop(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    registry.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-new", now=110)

    assert registry.release("sows/foo.md", instance_id="i-old", outcome="error", now=150) is False
    assert registry.get("sows/foo.md").status is RunStatus.WORKING


def test_list_active_excludes_terminal_and_expired(registry):
    registry.try_acquire("sows/a.md", dispatch_id="da", now=100, ttl_seconds=TTL)  # active
    registry.try_acquire("sows/b.md", dispatch_id="db", now=100, ttl_seconds=TTL)
    registry.release("sows/b.md", instance_id=None, outcome="success", now=150)  # terminal
    registry.try_acquire("sows/c.md", dispatch_id="dc", now=100, ttl_seconds=10)  # expires 110

    active = {r.sow for r in registry.list_active(now=500)}
    assert active == {"sows/a.md"}


# -- run history: the DynamoDB RUN# rows must honor the same contract ----


def test_history_row_written_on_acquire_and_finalized_on_release(registry):
    registry.try_acquire(
        "dx/cards.md", dispatch_id="d1", now=100, ttl_seconds=TTL, dispatched_by="alice"
    )
    registry.attach_instance("dx/cards.md", dispatch_id="d1", instance_id="i-1", now=110)
    registry.release("dx/cards.md", instance_id="i-1", outcome="success", now=460, prs_opened=3)

    history = registry.list_history("dx/cards.md")
    assert len(history) == 1
    row = history[0]
    assert row.doc_type == "dx"
    assert row.compute_type == "ec2"
    assert row.dispatched_by == "alice"
    assert row.instance_id == "i-1"
    assert row.status is RunStatus.DONE
    assert row.outcome == "success"
    assert row.finished_at == 460
    assert row.duration_seconds == 360
    assert row.prs_opened == 3


def test_history_rows_persist_across_redispatch(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    registry.release("sows/foo.md", instance_id=None, outcome="error", now=150)
    registry.try_acquire("sows/foo.md", dispatch_id="d2", now=2000, ttl_seconds=TTL)
    registry.release("sows/foo.md", instance_id=None, outcome="success", now=2100)

    history = registry.list_history("sows/foo.md")
    assert [r.dispatch_id for r in history] == ["d1", "d2"]
    assert [r.outcome for r in history] == ["error", "success"]


def test_history_row_has_no_ttl_attribute(registry):
    """RUN rows must omit expires_at so the table's TTL never reaps them."""
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)

    items = registry._table.scan().get("Items", [])
    run_rows = [i for i in items if i["sk"].startswith("RUN#")]
    assert run_rows and all("expires_at" not in i for i in run_rows)


def test_scan_history_returns_only_run_rows(registry):
    registry.try_acquire("sows/a.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    registry.attach_instance("sows/a.md", dispatch_id="d1", instance_id="i-1", now=110)
    registry.release("sows/a.md", instance_id="i-1", outcome="success", now=460, prs_opened=2)
    registry.try_acquire("dx/b.md", dispatch_id="d2", now=500, ttl_seconds=TTL)

    rows = registry.scan_history()

    # LOCK items are excluded; only the immutable RUN# rows come back.
    assert {(r.sow, r.dispatch_id) for r in rows} == {("sows/a.md", "d1"), ("dx/b.md", "d2")}
    finalized = next(r for r in rows if r.dispatch_id == "d1")
    assert finalized.outcome == "success"
    assert finalized.duration_seconds == 360
    assert finalized.prs_opened == 2
    assert finalized.doc_type == "sow"


def test_superseded_release_does_not_finalize_history(registry):
    registry.try_acquire("sows/foo.md", dispatch_id="d1", now=100, ttl_seconds=TTL)
    registry.attach_instance("sows/foo.md", dispatch_id="d1", instance_id="i-old", now=110)
    registry.try_acquire("sows/foo.md", dispatch_id="d2", now=2000, ttl_seconds=TTL)

    # Old worker's late release is a no-op; its history row stays WORKING.
    assert registry.release("sows/foo.md", instance_id="i-old", outcome="error", now=2100) is False
    by_dispatch = {r.dispatch_id: r for r in registry.list_history("sows/foo.md")}
    assert by_dispatch["d1"].status is RunStatus.WORKING
    assert by_dispatch["d1"].finished_at is None
