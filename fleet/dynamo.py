"""DynamoDB implementation of the RunRegistry.

The one-active-worker-per-SOW guarantee rides on a single conditional
write in ``try_acquire``: DynamoDB evaluates the condition atomically, so
two simultaneous dispatches cannot both succeed. Lock staleness is
enforced in the condition (``expires_at <= now``) rather than by relying
on TTL deletion, which DynamoDB applies only best-effort.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from fleet.models import AcquireResult, RunRecord, RunStatus
from fleet.registry import FleetError, RunRegistry

_CONDITIONAL_FAILED = "ConditionalCheckFailedException"


class DynamoRunRegistry(RunRegistry):
    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- reads ---------------------------------------------------------

    def get(self, sow: str) -> RunRecord | None:
        item = self._table.get_item(Key={"sow": sow}).get("Item")
        return _to_record(item) if item else None

    def list_active(self, now: int) -> list[RunRecord]:
        resp = self._table.scan(
            FilterExpression="#status = :working AND expires_at > :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":working": RunStatus.WORKING.value, ":now": now},
        )
        return [_to_record(i) for i in resp.get("Items", [])]

    # -- writes --------------------------------------------------------

    def try_acquire(
        self,
        sow: str,
        dispatch_id: str,
        now: int,
        ttl_seconds: int,
        dispatched_by: str | None = None,
    ) -> AcquireResult:
        record = RunRecord(
            sow=sow,
            status=RunStatus.WORKING,
            dispatch_id=dispatch_id,
            started_at=now,
            updated_at=now,
            expires_at=now + ttl_seconds,
            instance_id=None,
            dispatched_by=dispatched_by,
        )
        try:
            self._table.put_item(
                Item=_to_item(record),
                # Acquire iff: no current lock, OR it is terminal, OR it
                # has expired. Evaluated atomically by DynamoDB.
                ConditionExpression=(
                    "attribute_not_exists(sow) "
                    "OR #status IN (:done, :error, :timeout) "
                    "OR expires_at <= :now"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":done": RunStatus.DONE.value,
                    ":error": RunStatus.ERROR.value,
                    ":timeout": RunStatus.TIMEOUT.value,
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != _CONDITIONAL_FAILED:
                raise
            # Refused — surface the current holder for the operator message.
            return AcquireResult(acquired=False, conflict=self.get(sow))
        return AcquireResult(acquired=True, record=record)

    def attach_instance(
        self, sow: str, dispatch_id: str, instance_id: str, now: int
    ) -> RunRecord:
        try:
            resp = self._table.update_item(
                Key={"sow": sow},
                UpdateExpression="SET instance_id = :iid, updated_at = :now",
                ConditionExpression="dispatch_id = :did",
                ExpressionAttributeValues={
                    ":iid": instance_id,
                    ":now": now,
                    ":did": dispatch_id,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != _CONDITIONAL_FAILED:
                raise
            raise FleetError(
                f"cannot attach instance to {sow!r}: not held by dispatch {dispatch_id!r}"
            ) from None
        return _to_record(resp["Attributes"])

    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
    ) -> bool:
        status = RunStatus.from_outcome(outcome)
        values = {":status": status.value, ":now": now}
        if force:
            condition = "attribute_exists(sow)"
        elif instance_id is None:
            condition = "attribute_exists(sow) AND attribute_not_exists(instance_id)"
        else:
            condition = "instance_id = :iid"
            values[":iid"] = instance_id
        try:
            self._table.update_item(
                Key={"sow": sow},
                UpdateExpression="SET #status = :status, updated_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != _CONDITIONAL_FAILED:
                raise
            return False
        return True


def _to_item(record: RunRecord) -> dict:
    item = {
        "sow": record.sow,
        "status": RunStatus(record.status).value,
        "dispatch_id": record.dispatch_id,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "expires_at": record.expires_at,
    }
    if record.instance_id is not None:
        item["instance_id"] = record.instance_id
    if record.dispatched_by is not None:
        item["dispatched_by"] = record.dispatched_by
    return item


def _to_record(item: dict) -> RunRecord:
    return RunRecord(
        sow=item["sow"],
        status=RunStatus(item["status"]),
        dispatch_id=item["dispatch_id"],
        started_at=int(item["started_at"]),
        updated_at=int(item["updated_at"]),
        expires_at=int(item["expires_at"]),
        instance_id=item.get("instance_id"),
        dispatched_by=item.get("dispatched_by"),
    )
