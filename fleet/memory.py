"""In-memory RunRegistry — for tests and local dry-runs.

Implements the exact locking contract the DynamoDB version enforces via
conditional writes, so the contract tests in test_fleet_registry.py can
run without AWS.
"""

from __future__ import annotations

from dataclasses import replace

from fleet.models import AcquireResult, RunRecord, RunStatus
from fleet.registry import FleetError, RunRegistry


class FakeRunRegistry(RunRegistry):
    def __init__(self) -> None:
        self._rows: dict[str, RunRecord] = {}

    def try_acquire(
        self,
        sow: str,
        dispatch_id: str,
        now: int,
        ttl_seconds: int,
        dispatched_by: str | None = None,
    ) -> AcquireResult:
        existing = self._rows.get(sow)
        if existing is not None and existing.is_active(now):
            return AcquireResult(acquired=False, conflict=existing)

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
        self._rows[sow] = record
        return AcquireResult(acquired=True, record=record)

    def attach_instance(
        self, sow: str, dispatch_id: str, instance_id: str, now: int
    ) -> RunRecord:
        existing = self._rows.get(sow)
        if existing is None or existing.dispatch_id != dispatch_id:
            raise FleetError(
                f"cannot attach instance to {sow!r}: not held by dispatch {dispatch_id!r}"
            )
        updated = replace(existing, instance_id=instance_id, updated_at=now)
        self._rows[sow] = updated
        return updated

    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
    ) -> bool:
        existing = self._rows.get(sow)
        if existing is None:
            return False
        if not force and existing.instance_id != instance_id:
            return False
        self._rows[sow] = replace(
            existing, status=RunStatus.from_outcome(outcome), updated_at=now
        )
        return True

    def get(self, sow: str) -> RunRecord | None:
        return self._rows.get(sow)

    def list_active(self, now: int) -> list[RunRecord]:
        return [r for r in self._rows.values() if r.is_active(now)]
