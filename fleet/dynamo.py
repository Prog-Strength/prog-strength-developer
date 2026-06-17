"""DynamoDB implementation of the RunRegistry.

The table holds two kinds of item per ticket, discriminated by sort key:

* the **lock item** (``sk = "LOCK"``) — one mutable row whose conditional
  write enforces the one-active-worker-per-SOW guarantee. DynamoDB
  evaluates the condition atomically, so two simultaneous dispatches
  cannot both acquire. Staleness is enforced in the condition
  (``expires_at <= now``), not by TTL deletion, which is best-effort.
* **run-history items** (``sk = "RUN#<started_at>#<dispatch_id>"``) — one
  immutable row appended per dispatch, the durable record of every
  autonomous-developer session. They carry no ``expires_at``, so the
  table's TTL never reaps them.

Lock operations key on ``sk = "LOCK"``; history operations append/patch
the RUN row the lock currently points at.
"""

from __future__ import annotations

import boto3
from botocore.exceptions import ClientError

from fleet.models import (
    AcquireResult,
    RunHistory,
    RunRecord,
    RunStatus,
    doc_type_for_path,
)
from fleet.registry import FleetError, RunRegistry

_CONDITIONAL_FAILED = "ConditionalCheckFailedException"

#: Sort key of the single lock item in each ticket's partition.
_LOCK_SK = "LOCK"
#: Sort-key prefix for the immutable run-history items.
_RUN_PREFIX = "RUN#"


def _run_sk(started_at: int, dispatch_id: str) -> str:
    """Time-sortable sort key for a run-history item. ``started_at`` is
    zero-padded so lexical order matches chronological order."""
    return f"{_RUN_PREFIX}{started_at:020d}#{dispatch_id}"


class DynamoRunRegistry(RunRegistry):
    def __init__(self, table_name: str, region: str) -> None:
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    # -- reads ---------------------------------------------------------

    def get(self, sow: str) -> RunRecord | None:
        item = self._table.get_item(Key={"sow": sow, "sk": _LOCK_SK}).get("Item")
        return _to_record(item) if item else None

    def list_active(self, now: int) -> list[RunRecord]:
        resp = self._table.scan(
            FilterExpression="sk = :lock AND #status = :working AND expires_at > :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":lock": _LOCK_SK,
                ":working": RunStatus.WORKING.value,
                ":now": now,
            },
        )
        return [_to_record(i) for i in resp.get("Items", [])]

    def list_history(self, sow: str) -> list[RunHistory]:
        resp = self._table.query(
            KeyConditionExpression="sow = :sow AND begins_with(sk, :run)",
            ExpressionAttributeValues={":sow": sow, ":run": _RUN_PREFIX},
        )
        return [_to_history(i) for i in resp.get("Items", [])]

    def scan_history(self) -> list[RunHistory]:
        # Full-table scan filtered to RUN# rows. Paginated so the whole
        # durable record is returned regardless of size; at a handful of
        # dispatches a day this stays a cheap, infrequent (60s) read.
        rows: list[RunHistory] = []
        kwargs: dict = {
            "FilterExpression": "begins_with(sk, :run)",
            "ExpressionAttributeValues": {":run": _RUN_PREFIX},
        }
        while True:
            resp = self._table.scan(**kwargs)
            rows.extend(_to_history(i) for i in resp.get("Items", []))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                return rows
            kwargs["ExclusiveStartKey"] = start_key

    # -- writes --------------------------------------------------------

    def try_acquire(
        self,
        sow: str,
        dispatch_id: str,
        now: int,
        ttl_seconds: int,
        dispatched_by: str | None = None,
        doc_type: str | None = None,
        compute_type: str = "ec2",
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
                Item=_lock_item(record),
                # Acquire iff this ticket's lock item is absent, terminal,
                # or expired. Evaluated atomically by DynamoDB.
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
        # Lock won — append the immutable run-history row for this dispatch.
        history = RunHistory(
            sow=sow,
            dispatch_id=dispatch_id,
            doc_type=doc_type if doc_type is not None else doc_type_for_path(sow),
            status=RunStatus.WORKING,
            started_at=now,
            updated_at=now,
            compute_type=compute_type,
            dispatched_by=dispatched_by,
        )
        self._table.put_item(Item=_history_item(history))
        return AcquireResult(acquired=True, record=record)

    def attach_instance(
        self, sow: str, dispatch_id: str, instance_id: str, now: int
    ) -> RunRecord:
        try:
            resp = self._table.update_item(
                Key={"sow": sow, "sk": _LOCK_SK},
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
        lock = resp["Attributes"]
        # Patch the same dispatch's run-history row (keyed by its started_at).
        self._table.update_item(
            Key={"sow": sow, "sk": _run_sk(int(lock["started_at"]), dispatch_id)},
            UpdateExpression="SET instance_id = :iid, updated_at = :now",
            ExpressionAttributeValues={":iid": instance_id, ":now": now},
        )
        return _to_record(lock)

    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
        prs_opened: int | None = None,
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
            resp = self._table.update_item(
                Key={"sow": sow, "sk": _LOCK_SK},
                UpdateExpression="SET #status = :status, updated_at = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != _CONDITIONAL_FAILED:
                raise
            return False
        # Lock released — finalize the run-history row it pointed at. Only
        # reached when the conditional passed, so a superseded worker's
        # no-op release never touches anyone's history.
        lock = resp["Attributes"]
        started_at = int(lock["started_at"])
        self._table.update_item(
            Key={"sow": sow, "sk": _run_sk(started_at, lock["dispatch_id"])},
            UpdateExpression=(
                "SET #status = :status, outcome = :outcome, "
                "finished_at = :now, duration_seconds = :dur, "
                "prs_opened = :prs, updated_at = :now"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status.value,
                ":outcome": outcome,
                ":now": now,
                ":dur": now - started_at,
                ":prs": prs_opened,
            },
        )
        return True


# -- (de)serialization -------------------------------------------------


def _lock_item(record: RunRecord) -> dict:
    item = {
        "sow": record.sow,
        "sk": _LOCK_SK,
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


def _history_item(history: RunHistory) -> dict:
    # No expires_at: run rows are permanent and must never be TTL-reaped.
    item = {
        "sow": history.sow,
        "sk": _run_sk(history.started_at, history.dispatch_id),
        "dispatch_id": history.dispatch_id,
        "doc_type": history.doc_type,
        "compute_type": history.compute_type,
        "status": RunStatus(history.status).value,
        "started_at": history.started_at,
        "updated_at": history.updated_at,
    }
    if history.dispatched_by is not None:
        item["dispatched_by"] = history.dispatched_by
    return item


def _to_history(item: dict) -> RunHistory:
    def _opt_int(key: str) -> int | None:
        value = item.get(key)
        return int(value) if value is not None else None

    return RunHistory(
        sow=item["sow"],
        dispatch_id=item["dispatch_id"],
        doc_type=item["doc_type"],
        status=RunStatus(item["status"]),
        started_at=int(item["started_at"]),
        updated_at=int(item["updated_at"]),
        compute_type=item.get("compute_type", "ec2"),
        instance_id=item.get("instance_id"),
        dispatched_by=item.get("dispatched_by"),
        outcome=item.get("outcome"),
        finished_at=_opt_int("finished_at"),
        duration_seconds=_opt_int("duration_seconds"),
        prs_opened=_opt_int("prs_opened"),
    )
