"""Orchestration over a RunRegistry: dispatch gating and the
human-readable messaging the CLI prints. Kept apart from arg-parsing so
the gate logic is reusable by a future manager service.
"""

from __future__ import annotations

from fleet.models import RunRecord


def conflict_message(holder: RunRecord) -> str:
    """One-line explanation of why a dispatch was refused."""
    where = f"instance {holder.instance_id}" if holder.instance_id else "a launching worker"
    return (
        f"SOW {holder.sow!r} is already being built by {where} "
        f"(started_at={holder.started_at}). Refusing to dispatch a second worker."
    )


def active_table(records: list[RunRecord]) -> str:
    """Render active runs for `fleet list`."""
    if not records:
        return "No workers are currently building a SOW."
    rows = sorted(records, key=lambda r: r.started_at)
    lines = [f"{len(rows)} active run(s):"]
    for r in rows:
        iid = r.instance_id or "(launching)"
        lines.append(f"  {r.sow}\t{iid}\tstarted_at={r.started_at}\texpires_at={r.expires_at}")
    return "\n".join(lines)
