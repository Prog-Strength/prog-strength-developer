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
            AttributeDefinitions=[{"AttributeName": "sow", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "sow", "KeyType": "HASH"}],
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
