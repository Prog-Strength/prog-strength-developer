"""Value types for the fleet run registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunStatus(str, Enum):
    """Lifecycle state of a worker's run on a SOW.

    Inherits from ``str`` so values serialize directly to DynamoDB and
    JSON without a custom encoder.
    """

    WORKING = "working"
    DONE = "done"
    ERROR = "error"
    TIMEOUT = "timeout"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @classmethod
    def from_outcome(cls, outcome: str) -> "RunStatus":
        """Map a worker outcome string to its terminal status.

        ``success`` is the one outcome whose status name differs from
        the outcome word (``done``); the rest map straight through.
        """
        mapping = {
            "success": cls.DONE,
            "done": cls.DONE,
            "error": cls.ERROR,
            "timeout": cls.TIMEOUT,
        }
        try:
            return mapping[outcome]
        except KeyError:
            raise ValueError(f"unknown outcome: {outcome!r}") from None


_TERMINAL = frozenset({RunStatus.DONE, RunStatus.ERROR, RunStatus.TIMEOUT})


@dataclass
class RunRecord:
    """One row in the run registry — the lock for a single SOW."""

    sow: str
    status: RunStatus
    dispatch_id: str
    started_at: int
    updated_at: int
    expires_at: int
    instance_id: str | None = None
    dispatched_by: str | None = None

    def is_active(self, now: int) -> bool:
        """A run holds the SOW iff it is WORKING and not past expiry."""
        return self.status is RunStatus.WORKING and self.expires_at > now


@dataclass
class AcquireResult:
    """Outcome of a ``try_acquire`` call.

    On success: ``acquired`` is True, ``record`` is the new lock,
    ``conflict`` is None. On refusal: ``acquired`` is False, ``record``
    is None, and ``conflict`` is the current holder (for messaging).
    """

    acquired: bool
    record: RunRecord | None = None
    conflict: RunRecord | None = None
