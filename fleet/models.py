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


#: Known top-level ticket directories → recordkeeping doc_type. The
#: dispatch workflow doesn't check out prog-strength-docs, so doc_type is
#: derived from the ticket path here rather than from frontmatter; the
#: convention is one directory per work type. New types add an entry.
_DOC_TYPE_BY_DIR = {
    "sows": "sow",
    "dx": "dx",
}


def doc_type_for_path(path: str) -> str:
    """Recordkeeping doc_type for a ticket path.

    Maps the leading directory: ``sows/…`` → ``"sow"``, ``dx/…`` → ``"dx"``.
    An unmapped directory is returned verbatim (so a new work type is
    recorded honestly until a mapping entry is added); a path with no
    directory returns the whole string.
    """
    head, sep, _ = path.partition("/")
    if not sep:
        return path
    return _DOC_TYPE_BY_DIR.get(head, head)


@dataclass
class RunHistory:
    """One immutable run-history row — the durable record of a single
    dispatch, distinct from the mutable per-ticket lock (:class:`RunRecord`).

    Created at acquire with ``status=working`` and the dispatch metadata,
    patched with ``instance_id`` at attach, and finalized at release with
    the terminal ``outcome``/``finished_at``/``duration_seconds``/
    ``prs_opened``. A row left at ``working`` with no ``finished_at`` is a
    run that died or was superseded before releasing.
    """

    sow: str
    dispatch_id: str
    doc_type: str
    status: RunStatus
    started_at: int
    updated_at: int
    compute_type: str = "ec2"
    instance_id: str | None = None
    dispatched_by: str | None = None
    outcome: str | None = None
    finished_at: int | None = None
    duration_seconds: int | None = None
    prs_opened: int | None = None


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
