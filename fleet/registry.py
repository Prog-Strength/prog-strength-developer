"""The RunRegistry interface and its errors.

A registry records which SOW each worker is building and enforces the
one-active-worker-per-SOW invariant. Implementations: ``memory`` (tests
/ local) and ``dynamo`` (production). Both honor the same contract,
covered by tests/test_fleet_registry.py.

All methods take ``now`` (epoch seconds) explicitly rather than reading
the clock, so behavior is deterministic and testable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fleet.models import AcquireResult, RunRecord


class FleetError(Exception):
    """Base error for fleet operations (e.g. attaching to a lock you
    don't own)."""


class RunRegistry(ABC):
    @abstractmethod
    def try_acquire(
        self,
        sow: str,
        dispatch_id: str,
        now: int,
        ttl_seconds: int,
        dispatched_by: str | None = None,
    ) -> AcquireResult:
        """Atomically claim ``sow`` for ``dispatch_id``.

        Succeeds when the SOW is unheld, terminal, or its lock has
        expired. Otherwise returns ``acquired=False`` with the current
        holder in ``conflict``. Never blocks; never half-writes.
        """

    @abstractmethod
    def attach_instance(
        self, sow: str, dispatch_id: str, instance_id: str, now: int
    ) -> RunRecord:
        """Record the launched ``instance_id`` on the lock this dispatch
        holds. Raises :class:`FleetError` if the lock is missing or owned
        by a different dispatch."""

    @abstractmethod
    def release(
        self,
        sow: str,
        instance_id: str | None,
        outcome: str,
        now: int,
        force: bool = False,
    ) -> bool:
        """Mark the run terminal, freeing the SOW. Returns True if
        released. A non-matching ``instance_id`` is a no-op (returns
        False) so a superseded worker can't free the new owner's lock —
        unless ``force`` is set (operator override)."""

    @abstractmethod
    def get(self, sow: str) -> RunRecord | None:
        """Return the current record for ``sow``, or None."""

    @abstractmethod
    def list_active(self, now: int) -> list[RunRecord]:
        """Return all runs currently holding a SOW (WORKING, unexpired)."""
