"""In-memory RunRegistry — for tests and local dry-runs.

Implements the exact locking contract the DynamoDB version enforces via
conditional writes, so the contract tests in test_fleet_registry.py can
run without AWS.
"""

from __future__ import annotations

from dataclasses import replace

from fleet.models import (
    AcquireResult,
    RunHistory,
    RunRecord,
    RunStatus,
    doc_type_for_path,
)
from fleet.registry import FleetError, RunRegistry


class FakeRunRegistry(RunRegistry):
    def __init__(self) -> None:
        self._rows: dict[str, RunRecord] = {}
        # Immutable run-history rows keyed (sow, dispatch_id) — the
        # in-memory analogue of the RUN# items the DynamoDB table appends.
        self._history: dict[tuple[str, str], RunHistory] = {}

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
        self._history[(sow, dispatch_id)] = RunHistory(
            sow=sow,
            dispatch_id=dispatch_id,
            doc_type=doc_type if doc_type is not None else doc_type_for_path(sow),
            status=RunStatus.WORKING,
            started_at=now,
            updated_at=now,
            compute_type=compute_type,
            dispatched_by=dispatched_by,
        )
        return AcquireResult(acquired=True, record=record)

    def attach_instance(
        self,
        sow: str,
        dispatch_id: str,
        instance_id: str,
        now: int,
        compute_type: str | None = None,
    ) -> RunRecord:
        existing = self._rows.get(sow)
        if existing is None or existing.dispatch_id != dispatch_id:
            raise FleetError(
                f"cannot attach instance to {sow!r}: not held by dispatch {dispatch_id!r}"
            )
        updated = replace(existing, instance_id=instance_id, updated_at=now)
        self._rows[sow] = updated
        hist = self._history.get((sow, dispatch_id))
        if hist is not None:
            extra = {"compute_type": compute_type} if compute_type is not None else {}
            self._history[(sow, dispatch_id)] = replace(
                hist, instance_id=instance_id, updated_at=now, **extra
            )
        return updated

    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
        prs_opened: int | None = None,
    ) -> bool:
        existing = self._rows.get(sow)
        if existing is None:
            return False
        if not force and existing.instance_id != instance_id:
            return False
        self._rows[sow] = replace(
            existing, status=RunStatus.from_outcome(outcome), updated_at=now
        )
        # Finalize the run-history row the lock currently points at — the
        # run that actually held the SOW, not necessarily ``instance_id``.
        hist = self._history.get((sow, existing.dispatch_id))
        if hist is not None:
            self._history[(sow, existing.dispatch_id)] = replace(
                hist,
                status=RunStatus.from_outcome(outcome),
                outcome=outcome,
                finished_at=now,
                duration_seconds=now - hist.started_at,
                prs_opened=prs_opened,
                updated_at=now,
            )
        return True

    def get(self, sow: str) -> RunRecord | None:
        return self._rows.get(sow)

    def list_active(self, now: int) -> list[RunRecord]:
        return [r for r in self._rows.values() if r.is_active(now)]

    def list_history(self, sow: str) -> list[RunHistory]:
        rows = [h for (s, _), h in self._history.items() if s == sow]
        return sorted(rows, key=lambda r: r.started_at)

    def scan_history(self) -> list[RunHistory]:
        return sorted(self._history.values(), key=lambda r: r.started_at)
